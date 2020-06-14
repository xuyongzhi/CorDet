import torch
import torch.nn as nn
import torch.utils.checkpoint as cp
from mmcv.cnn import constant_init, kaiming_init
from mmcv.runner import load_checkpoint
from torch.nn.modules.batchnorm import _BatchNorm

from mmdet.models.plugins import GeneralizedAttention
from mmdet.ops import ContextBlock, DeformConv, ModulatedDeformConv
from ..registry import BACKBONES
from ..utils import build_conv_layer, build_norm_layer
import MinkowskiEngine as ME
from ..utils.mink_vox_common import mink_max_pool
from MinkowskiEngine import SparseTensor
import numpy as np

from tools import debug_utils
import math
import time

RECORD_T = 0
SHOW_NET = 0

from configs.common import DEBUG_CFG

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self,
                 inplanes,
                 planes,
                 stride=1,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=dict(type='MinkConv'),
                 norm_cfg=dict(type='MinkBN'),
                 dcn=None,
                 gcb=None,
                 gen_attention=None):
        super(BasicBlock, self).__init__()
        assert dcn is None, "Not implemented yet."
        assert gen_attention is None, "Not implemented yet."
        assert gcb is None, "Not implemented yet."

        self.norm1_name, norm1 = build_norm_layer(norm_cfg, planes, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(norm_cfg, planes, postfix=2)

        self.conv1 = build_conv_layer(
            conv_cfg,
            inplanes,
            planes,
            3,
            stride=stride,
            padding=dilation,
            dilation=dilation,
            bias=False)
        self.add_module(self.norm1_name, norm1)
        self.conv2 = build_conv_layer(
            conv_cfg, planes, planes, 3, padding=1, bias=False)
        self.add_module(self.norm2_name, norm2)

        self.relu = ME.MinkowskiReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride
        self.dilation = dilation
        assert not with_cp

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    @property
    def norm2(self):
        return getattr(self, self.norm2_name)

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.norm1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.norm2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self,
                 inplanes,
                 planes,
                 stride=1,
                 z_stride=1,
                 z_kernel=3,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=dict(type='MinkConv'),
                 norm_cfg=dict(type='MinkBN'),
                 dcn=None,
                 gcb=None,
                 gen_attention=None,
                 max_planes=2048):
        """Bottleneck block for Sparse3DResNet.
        If style is "pytorch", the stride-two layer is the 3x3 conv layer,
        if it is "caffe", the stride-two layer is the first 1x1 conv layer.
        """
        super(Bottleneck, self).__init__()
        assert style in ['pytorch', 'caffe']
        assert dcn is None or isinstance(dcn, dict)
        assert gcb is None or isinstance(gcb, dict)
        assert gen_attention is None or isinstance(gen_attention, dict)

        self.inplanes = inplanes
        self.max_planes = max_planes
        self.planes = planes
        self.stride = stride
        self.z_stride = z_stride
        self.z_kernel = z_kernel
        self.dilation = dilation
        self.style = style
        self.with_cp = with_cp
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.dcn = dcn
        self.with_dcn = dcn is not None
        self.gcb = gcb
        self.with_gcb = gcb is not None
        self.gen_attention = gen_attention
        self.with_gen_attention = gen_attention is not None

        if DEBUG_CFG.SPARSE_BEV:
          stride = (stride, stride, 1)
          kernel_2 = (3,3,1)
        else:
          stride = (stride, stride, z_stride)
          kernel_2 = (3,3,z_kernel)

        if self.style == 'pytorch':
            self.conv1_stride = 1
            self.conv2_stride = stride
        else:
            self.conv1_stride = stride
            self.conv2_stride = 1

        self.norm1_name, norm1 = build_norm_layer(norm_cfg, planes, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(norm_cfg, planes, postfix=2)
        self.norm3_name, norm3 = build_norm_layer(
            norm_cfg, min(self.max_planes, planes * self.expansion), postfix=3)

        self.conv1 = build_conv_layer(
            conv_cfg,
            inplanes,
            planes,
            kernel_size=1,
            stride=self.conv1_stride,
            bias=False)
        self.add_module(self.norm1_name, norm1)
        fallback_on_stride = False
        self.with_modulated_dcn = False
        if self.with_dcn:
            fallback_on_stride = dcn.get('fallback_on_stride', False)
            self.with_modulated_dcn = dcn.get('modulated', False)

        if not self.with_dcn or fallback_on_stride:
            self.conv2 = build_conv_layer(
                conv_cfg,
                planes,
                planes,
                kernel_size=kernel_2,
                stride=self.conv2_stride,
                padding=dilation,
                dilation=dilation,
                bias=False)
        else:
            assert conv_cfg is None, 'conv_cfg must be None for DCN'
            self.deformable_groups = dcn.get('deformable_groups', 1)
            if not self.with_modulated_dcn:
                conv_op = DeformConv
                offset_channels = 18
            else:
                conv_op = ModulatedDeformConv
                offset_channels = 27
            self.conv2_offset = nn.Conv2d(
                planes,
                self.deformable_groups * offset_channels,
                kernel_size=kernel_2,
                stride=self.conv2_stride,
                padding=dilation,
                dilation=dilation)
            self.conv2 = conv_op(
                planes,
                planes,
                kernel_size=kernel_2,
                stride=self.conv2_stride,
                padding=dilation,
                dilation=dilation,
                deformable_groups=self.deformable_groups,
                bias=False)
        self.add_module(self.norm2_name, norm2)
        self.conv3 = build_conv_layer(
            conv_cfg,
            planes,
            min(self.max_planes, planes * self.expansion),
            kernel_size=1,
            bias=False)
        self.add_module(self.norm3_name, norm3)

        self.relu = ME.MinkowskiReLU(inplace=True)
        self.downsample = downsample

        if self.with_gcb:
            gcb_inplanes = min(self.max_planes, planes * self.expansion)
            self.context_block = ContextBlock(inplanes=gcb_inplanes, **gcb)

        # gen_attention
        if self.with_gen_attention:
            self.gen_attention_block = GeneralizedAttention(
                planes, **gen_attention)

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    @property
    def norm2(self):
        return getattr(self, self.norm2_name)

    @property
    def norm3(self):
        return getattr(self, self.norm3_name)

    def forward(self, x):

        def _inner_forward(x):
            identity = x
            out = self.conv1(x)
            out = self.norm1(out)
            out = self.relu(out)

            if not self.with_dcn:
                out = self.conv2(out)
            elif self.with_modulated_dcn:
                offset_mask = self.conv2_offset(out)
                offset = offset_mask[:, :18 * self.deformable_groups, :, :]
                mask = offset_mask[:, -9 * self.deformable_groups:, :, :]
                mask = mask.sigmoid()
                out = self.conv2(out, offset, mask)
            else:
                offset = self.conv2_offset(out)
                out = self.conv2(out, offset)
            out = self.norm2(out)
            out = self.relu(out)

            if self.with_gen_attention:
                out = self.gen_attention_block(out)

            out = self.conv3(out)
            out = self.norm3(out)

            if self.with_gcb:
                out = self.context_block(out)

            if self.downsample is not None:
                identity = self.downsample(x)

            out += identity

            return out

        if self.with_cp and x.requires_grad:
            out = cp.checkpoint(_inner_forward, x)
        else:
            out = _inner_forward(x)

        out = self.relu(out)

        return out


def make_vox_res_layer(block,
                   inplanes,
                   planes,
                   blocks,
                   stride=1,
                   z_stride=1,
                   z_kernel=3,
                   dilation=1,
                   style='pytorch',
                   with_cp=False,
                   conv_cfg=dict(type='MinkConv'),
                   norm_cfg=dict(type='MinkBN'),
                   dcn=None,
                   gcb=None,
                   gen_attention=None,
                   gen_attention_blocks=[],
                   max_planes=2048):
    downsample = None
    if stride != 1 or inplanes != min(max_planes,planes * block.expansion):
        if DEBUG_CFG.SPARSE_BEV:
          stride_ds = (stride,stride,1)
        else:
          stride_ds = (stride, stride, z_stride)
        downsample = nn.Sequential(
            build_conv_layer(
                conv_cfg,
                inplanes,
                min(max_planes, planes * block.expansion),
                kernel_size=1,
                stride=stride_ds,
                bias=False),
            build_norm_layer(norm_cfg, min(max_planes, planes * block.expansion))[1],
        )

    layers = []
    layers.append(
        block(
            inplanes=inplanes,
            planes=planes,
            stride=stride,
            z_stride=z_stride,
            z_kernel=z_kernel,
            dilation=dilation,
            downsample=downsample,
            style=style,
            with_cp=with_cp,
            conv_cfg=conv_cfg,
            norm_cfg=norm_cfg,
            dcn=dcn,
            gcb=gcb,
            gen_attention=gen_attention if
            (0 in gen_attention_blocks) else None,
            max_planes = max_planes
        )
    )
    inplanes = min(max_planes, planes * block.expansion)
    for i in range(1, blocks):
        layers.append(
            block(
                inplanes=inplanes,
                planes=planes,
                stride=1,
                z_kernel=z_kernel,
                dilation=dilation,
                style=style,
                with_cp=with_cp,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                dcn=dcn,
                gcb=gcb,
                gen_attention=gen_attention if
                (i in gen_attention_blocks) else None,
                max_planes = max_planes)
        )

    return nn.Sequential(*layers)


@BACKBONES.register_module
class Sparse3DResNet(nn.Module):
    """Sparse3DResNet backbone.

    Args:
        depth (int): Depth of resnet, from {18, 34, 50, 101, 152}.
        in_channels (int): Number of input image channels. Normally 3.
        num_stages (int): Resnet stages, normally 4.
        strides (Sequence[int]): Strides of the first block of each stage.
        dilations (Sequence[int]): Dilation of each stage.
        out_indices (Sequence[int]): Output from which stages.
        style (str): `pytorch` or `caffe`. If set to "pytorch", the stride-two
            layer is the 3x3 conv layer, otherwise the stride-two layer is
            the first 1x1 conv layer.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters.
        norm_cfg (dict): dictionary to construct and config norm layer.
        norm_eval (bool): Whether to set norm layers to eval mode, namely,
            freeze running stats (mean and var). Note: Effect on Batch Norm
            and its variants only.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed.
        zero_init_residual (bool): whether to use zero init for last norm layer
            in resblocks to let them behave as identity.

    Example:
        >>> from mmdet.models import Sparse3DResNet
        >>> import torch
        >>> self = Sparse3DResNet(depth=18)
        >>> self.eval()
        >>> inputs = torch.rand(1, 3, 32, 32)
        >>> level_outputs = self.forward(inputs)
        >>> for level_out in level_outputs:
        ...     print(tuple(level_out.shape))
        (1, 64, 8, 8)
        (1, 128, 4, 4)
        (1, 256, 2, 2)
        (1, 512, 1, 1)
    """

    arch_settings = {
        18: (BasicBlock, (2, 2, 2, 2)),
        34: (BasicBlock, (3, 4, 6, 3)),
        50: (Bottleneck, (3, 4, 6, 3)),
        101: (Bottleneck, (3, 4, 23, 3)),
        152: (Bottleneck, (3, 8, 36, 3))
    }

    def __init__(self,
                 depth,
                 in_channels=3,
                 num_stages=4,
                 strides=(1, 2, 2, 2),
                 z_strides=(3, 3, 3, 3),
                 dilations=(1, 1, 1, 1),
                 out_indices=(0, 1, 2, 3),
                 style='pytorch',
                 frozen_stages=-1,
                 conv_cfg=dict(type='MinkConv'),
                 norm_cfg=dict(type='MinkBN', requires_grad=True),
                 norm_eval=True,
                 dcn=None,
                 stage_with_dcn=(False, False, False, False),
                 gcb=None,
                 stage_with_gcb=(False, False, False, False),
                 gen_attention=None,
                 stage_with_gen_attention=((), (), (), ()),
                 with_cp=False,
                 zero_init_residual=True,
                 basic_planes = 64,
                 max_planes = 1024,
                 voxel_size = 0.04,
                 full_height  = 10.24,
                 stem_stride = None,
                 stem_stride_z = None,
                 z_out_dims = (),
                 ):
        super(Sparse3DResNet, self).__init__()
        assert len(z_out_dims) == num_stages + 1

        if depth not in self.arch_settings:
            raise KeyError('invalid depth {} for resnet'.format(depth))
        self.depth = depth
        self.num_stages = num_stages
        assert num_stages >= 1 and num_stages <= 4
        self.strides = strides
        self.z_strides = z_strides
        self.dilations = dilations
        assert len(strides) == len(dilations) == num_stages
        self.out_indices = out_indices
        assert max(out_indices) < num_stages
        self.style = style
        self.frozen_stages = frozen_stages
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.with_cp = with_cp
        self.norm_eval = norm_eval
        self.dcn = dcn
        self.stage_with_dcn = stage_with_dcn
        if dcn is not None:
            assert len(stage_with_dcn) == num_stages
        self.gen_attention = gen_attention
        self.gcb = gcb
        self.stage_with_gcb = stage_with_gcb
        if gcb is not None:
            assert len(stage_with_gcb) == num_stages
        self.zero_init_residual = zero_init_residual
        self.block, stage_blocks = self.arch_settings[depth]
        self.stage_blocks = stage_blocks[:num_stages]
        self.basic_planes = basic_planes
        if not DEBUG_CFG.SPARSE_BEV:
          self.inplanes = basic_planes * 2
        else:
          self.inplanes = basic_planes
        self.max_planes = max_planes

        self.voxel_size = voxel_size
        self.full_height = full_height
        self.stem_stride = stem_stride
        self.stem_stride_z_design = stem_stride_z
        self.z_out_dims = z_out_dims
        self.z_kernels = [min(3,d) for d in z_out_dims][:-1]

        self._make_stem_layer(in_channels)

        self.res_layers = []
        for i, num_blocks in enumerate(self.stage_blocks):
            stride = strides[i]
            z_stride = z_strides[i]
            dilation = dilations[i]
            dcn = self.dcn if self.stage_with_dcn[i] else None
            gcb = self.gcb if self.stage_with_gcb[i] else None
            planes = self.basic_planes * 2**i
            res_layer = make_vox_res_layer(
                self.block,
                self.inplanes,
                planes,
                num_blocks,
                stride=stride,
                z_stride=z_stride,
                z_kernel=self.z_kernels[i],
                dilation=dilation,
                style=self.style,
                with_cp=with_cp,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                dcn=dcn,
                gcb=gcb,
                gen_attention=gen_attention,
                gen_attention_blocks=stage_with_gen_attention[i],
                max_planes = self.max_planes)
            self.inplanes = min(self.max_planes, planes * self.block.expansion)
            layer_name = 'layer{}'.format(i + 1)
            self.add_module(layer_name, res_layer)
            self.res_layers.append(layer_name)


        # the strides of out layers
        self.out_strides = []
        for i in self.out_indices:
          xy_stride = 2**(i+0) * self.stem_stride
          z_stride = 2**(i+0) * self.stem_stride_z
          self.out_strides.append(np.array([xy_stride, xy_stride, z_stride]))

        self._freeze_stages()

        p = min( 2**(len(self.stage_blocks) - 1), self.max_planes)
        self.feat_dim = self.block.expansion * self.basic_planes * p


    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    def _make_stem_layer(self, in_channels):
        self.conv1s = []
        if self.stem_stride == 4:
          s0, s1 = 2, 2
        if self.stem_stride == 2:
          s0, s1 = 2, 1
        if self.stem_stride == 1:
          s0, s1 = 1, 1

        if not DEBUG_CFG.SPARSE_BEV:
          kernels = [(3,3,5),   (3,3,5), (3,3,3)]
          strides = [(s0,s0,2), (1,1,2), (s1,s1,2)]
          paddings = [(1,1,0), (1,1,0), (1,1,0)]
        else:
          kernels = [(7,7,1),]
          strides = [(s0, s0,1),]
          paddings = [ (3,3,0) ]

        out_planes = [self.basic_planes, self.basic_planes*2]

        num_layers = len(kernels) - 1
        in_channels_i = in_channels

        self.conv1s = []
        self.norm1s = []
        for i in range(num_layers):
          conv1_i = build_conv_layer(
              self.conv_cfg,
              in_channels_i,
              out_planes[i],
              kernel_size=kernels[i],
              stride=strides[i],
              padding=paddings[i],
              bias=False)
          in_channels_i = out_planes[i]
          norm1_name, norm1 = build_norm_layer(self.norm_cfg, in_channels_i, postfix=i+1)
          self.add_module(f'conv1_{i}', conv1_i)
          self.add_module(norm1_name, norm1)

          self.conv1s.append(conv1_i)
          self.norm1s.append(norm1)

        self.relu = ME.MinkowskiReLU(inplace=True)
        if not DEBUG_CFG.SPARSE_BEV:
          self.maxpool = mink_max_pool(kernel_size=3, stride=(s1, s1, 2), padding=(1,1,0))
        else:
          self.maxpool = mink_max_pool(kernel_size=(3,3,1), stride=(s1,s1,1), padding=1)

        self.stem_stride_z = np.product( [s[-1] for s in strides] )
        assert self.stem_stride_z == self.stem_stride_z_design
        assert self.stem_stride == np.product( [s[0] for s in strides] )

    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            self.norm1.eval()
            for m in [self.conv1, self.norm1]:
                for param in m.parameters():
                    param.requires_grad = False

        for i in range(1, self.frozen_stages + 1):
            m = getattr(self, 'layer{}'.format(i))
            m.eval()
            import pdb; pdb.set_trace()  # XXX BREAKPOINT
            for param in m.parameters():
                param.requires_grad = False

    def init_weights(self, pretrained=None):
        if isinstance(pretrained, str):
            from mmdet.apis import get_root_logger
            logger = get_root_logger()
            load_checkpoint(self, pretrained, strict=False, logger=logger)
        elif pretrained is None:
            for m in self.modules():
                if isinstance(m, nn.Conv2d):
                    kaiming_init(m)
                elif isinstance(m, (_BatchNorm, nn.GroupNorm)):
                    constant_init(m, 1)

            if self.dcn is not None:
                for m in self.modules():
                    if isinstance(m, Bottleneck) and hasattr(
                            m, 'conv2_offset'):
                        constant_init(m.conv2_offset, 0)

            if self.zero_init_residual:
                for m in self.modules():
                    if isinstance(m, Bottleneck):
                        constant_init(m.norm3, 0)
                    elif isinstance(m, BasicBlock):
                        constant_init(m.norm2, 0)
        else:
            raise TypeError('pretrained must be a str or None')


    def forward(self, x, gt_bboxes):
        #debug_utils.show_shapes(x, 'img')

        if RECORD_T:
          ts = []
          ts.append(time.time())
          t0 = time.time()
          print('\n\n')
        if SHOW_NET:
          debug_utils._show_sparse_ls_shapes([x], 'res in')

        for i in range(len(self.conv1s)):
          #debug_utils._show_sparse_ls_shapes([x], f'{i}-0')
          #debug_utils._show_sparse_coords(x)
          x = self.conv1s[i](x)
          #debug_utils._show_sparse_ls_shapes([x], f'{i}-1')
          x = self.norm1s[i](x)
          #debug_utils._show_sparse_ls_shapes([x], f'{i}-2')
          #debug_utils._show_sparse_coords(x)
          x = self.relu(x)

          if RECORD_T:
            ts.append(time.time())
            t1 = time.time()
            #print(self.conv1s[i])
            print(f'conv1 {i}: {ts[-1]-ts[-2]:.3f}')
          if SHOW_NET:
            debug_utils._show_sparse_ls_shapes([x], f'conv1 {i}')

        x = self.maxpool(x)
        outs = []

        if RECORD_T:
          ts.append(time.time())
          print(f'max: {ts[-1]-ts[-2]:.3f}')
        if SHOW_NET:
          debug_utils._show_sparse_ls_shapes([x], 'max, stem out')

        for i, layer_name in enumerate(self.res_layers):
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)

            if RECORD_T:
              ts.append(time.time())
              print(f'res {i}: {ts[-1]-ts[-2]:.3f}')
            if SHOW_NET:
              debug_utils._show_sparse_ls_shapes([x], f'res {i}')


        outs_dense = []
        for i in range(len(outs)):
          dense_out ,_,_ = outs[i].dense()
          dense_out = dense_out.permute(0, 1, 3, 2, 4)
          outs_dense.append(dense_out)
          pass

        if DEBUG_CFG.VISUAL_RESNET_FEAT_OUT:
          debug_utils._show_sparse_coords(outs[0], gt_bboxes)
          for i in range(len(outs)):
            debug_utils._show_feats(outs_dense[i][...,0], gt_bboxes, self.stem_stride * 2**(i))

        if SHOW_NET:
          debug_utils._show_sparse_ls_shapes(outs, 'resnet outs')
          debug_utils._show_tensor_ls_shapes(outs_dense, 'resnet dense outs')
        if RECORD_T:
          ts.append(time.time())
          print(f'out: {ts[-1]-ts[-2]:.3f}')
          print(f'conv1: {t1-t0:.3f}')
          pass
        return tuple(outs_dense)

    def train(self, mode=True):
        super(Sparse3DResNet, self).train(mode)
        self._freeze_stages()
        if mode and self.norm_eval:
            for m in self.modules():
                # trick: eval have effect on BatchNorm only
                if isinstance(m, _BatchNorm):
                    m.eval()
