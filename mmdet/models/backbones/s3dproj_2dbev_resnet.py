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

from tools import debug_utils, visual_utils
import time
RECORD_T = 0
SHOW_NET = 0
from tools.debug_utils import _show_tensor_ls_shapes, _show_sparse_ls_shapes
from configs.common import DEBUG_CFG


def get_padding_same_featsize(kernel):
  if isinstance(kernel, tuple):
    padding = [int((k-1)/2) for k in kernel]
    padding = tuple(padding)
  else:
    padding = int((kernel-1)/2)
  return padding


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self,
                 inplanes,
                 planes,
                 kernel=3,
                 stride=1,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN'),
                 dcn=None,
                 gcb=None,
                 gen_attention=None):
        super(BasicBlock, self).__init__()
        assert dcn is None, "Not implemented yet."
        assert gen_attention is None, "Not implemented yet."
        assert gcb is None, "Not implemented yet."

        self.norm1_name, norm1 = build_norm_layer(norm_cfg, planes, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(norm_cfg, planes, postfix=2)
        padding = get_padding_same_featsize(kernel)

        self.conv1 = build_conv_layer(
            conv_cfg,
            inplanes,
            planes,
            kernel,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=False)
        self.add_module(self.norm1_name, norm1)
        self.conv2 = build_conv_layer(
            conv_cfg, planes, planes, kernel, padding=padding, bias=False)
        self.add_module(self.norm2_name, norm2)

        if conv_cfg is not None and conv_cfg['type'] == 'MinkConv':
          self.relu = ME.MinkowskiReLU(inplace=True)
        else:
          self.relu = nn.ReLU(inplace=True)
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
                 kernel=3,
                 stride=1,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN'),
                 dcn=None,
                 gcb=None,
                 gen_attention=None):
        """Bottleneck block for S3dProj_BevResNet.
        If style is "pytorch", the stride-two layer is the 3x3 conv layer,
        if it is "caffe", the stride-two layer is the first 1x1 conv layer.
        """
        super(Bottleneck, self).__init__()
        assert style in ['pytorch', 'caffe']
        assert dcn is None or isinstance(dcn, dict)
        assert gcb is None or isinstance(gcb, dict)
        assert gen_attention is None or isinstance(gen_attention, dict)

        self.inplanes = inplanes
        self.planes = planes
        self.stride = stride
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

        if self.style == 'pytorch':
            self.conv1_stride = 1
            self.conv2_stride = stride
        else:
            self.conv1_stride = stride
            self.conv2_stride = 1

        self.norm1_name, norm1 = build_norm_layer(norm_cfg, planes, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(norm_cfg, planes, postfix=2)
        self.norm3_name, norm3 = build_norm_layer(
            norm_cfg, planes * self.expansion, postfix=3)

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
                kernel_size=3,
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
                kernel_size=3,
                stride=self.conv2_stride,
                padding=dilation,
                dilation=dilation)
            self.conv2 = conv_op(
                planes,
                planes,
                kernel_size=3,
                stride=self.conv2_stride,
                padding=dilation,
                dilation=dilation,
                deformable_groups=self.deformable_groups,
                bias=False)
        self.add_module(self.norm2_name, norm2)
        self.conv3 = build_conv_layer(
            conv_cfg,
            planes,
            planes * self.expansion,
            kernel_size=1,
            bias=False)
        self.add_module(self.norm3_name, norm3)

        if conv_cfg is not None and conv_cfg['type'] == 'MinkConv':
          self.relu = ME.MinkowskiReLU(inplace=True)
        else:
          self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample

        if self.with_gcb:
            gcb_inplanes = planes * self.expansion
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


def make_res_layer(block,
                   inplanes,
                   planes,
                   blocks,
                   kernel=3,
                   stride=1,
                   dilation=1,
                   style='pytorch',
                   with_cp=False,
                   conv_cfg=None,
                   norm_cfg=dict(type='BN'),
                   dcn=None,
                   gcb=None,
                   gen_attention=None,
                   gen_attention_blocks=[]):
    downsample = None
    if stride != 1 or inplanes != planes * block.expansion:
        downsample = nn.Sequential(
            build_conv_layer(
                conv_cfg,
                inplanes,
                planes * block.expansion,
                kernel_size=1,
                stride=stride,
                bias=False),
            build_norm_layer(norm_cfg, planes * block.expansion)[1],
        )

    layers = []
    layers.append(
        block(
            inplanes=inplanes,
            planes=planes,
            kernel = kernel,
            stride=stride,
            dilation=dilation,
            downsample=downsample,
            style=style,
            with_cp=with_cp,
            conv_cfg=conv_cfg,
            norm_cfg=norm_cfg,
            dcn=dcn,
            gcb=gcb,
            gen_attention=gen_attention if
            (0 in gen_attention_blocks) else None))
    inplanes = planes * block.expansion
    for i in range(1, blocks):
        layers.append(
            block(
                inplanes=inplanes,
                planes=planes,
                kernel = kernel,
                stride=1,
                dilation=dilation,
                style=style,
                with_cp=with_cp,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                dcn=dcn,
                gcb=gcb,
                gen_attention=gen_attention if
                (i in gen_attention_blocks) else None))

    return nn.Sequential(*layers)


@BACKBONES.register_module
class S3dProj_BevResNet(nn.Module):
    """S3dProj_BevResNet backbone.

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
        >>> from mmdet.models import S3dProj_BevResNet
        >>> import torch
        >>> self = S3dProj_BevResNet(depth=18)
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
                 dilations=(1, 1, 1, 1),
                 out_indices=(0, 1, 2, 3),
                 style='pytorch',
                 frozen_stages=-1,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN', requires_grad=True),
                 norm_eval=True,
                 dcn=None,
                 stage_with_dcn=(False, False, False, False),
                 gcb=None,
                 stage_with_gcb=(False, False, False, False),
                 gen_attention=None,
                 stage_with_gen_attention=((), (), (), ()),
                 with_cp=False,
                 zero_init_residual=True,
                 basic_planes=64,
                 max_planes=2048,
                 stem_stride=4,
                 max_zdim=124,
                 bev_pad_pixels=0,
                 ):
        super(S3dProj_BevResNet, self).__init__()
        if depth not in self.arch_settings:
            raise KeyError('invalid depth {} for resnet'.format(depth))
        self.bev_pad_pixels = bev_pad_pixels
        self.max_zdim = max_zdim
        self.stem_stride = stem_stride
        self.depth = depth
        self.num_stages = num_stages
        assert num_stages >= 1 and num_stages <= 4
        self.strides = strides
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
        self.stage_with_gen_attention = stage_with_gen_attention
        self.gcb = gcb
        self.stage_with_gcb = stage_with_gcb
        if gcb is not None:
            assert len(stage_with_gcb) == num_stages
        self.zero_init_residual = zero_init_residual
        self.block, stage_blocks = self.arch_settings[depth]
        self.stage_blocks = stage_blocks[:num_stages]
        self.basic_planes = basic_planes
        self.max_planes = max_planes

        self.s3d_conv_cfg = dict(type='MinkConv')
        self.s3d_norm_cfg=dict(type='MinkBN', requires_grad=True)

        self._make_stem_layer(in_channels)
        self._make_project_layers()
        self.inplanes = self.prj_planes[-1]

        self.res_layers = []
        for i, num_blocks in enumerate(self.stage_blocks):
            stride = strides[i]
            dilation = dilations[i]
            dcn = self.dcn if self.stage_with_dcn[i] else None
            gcb = self.gcb if self.stage_with_gcb[i] else None
            planes = min( self.basic_planes * 2**i, self.max_planes)
            res_layer = make_res_layer(
                self.block,
                self.inplanes,
                planes,
                num_blocks,
                stride=stride,
                dilation=dilation,
                style=self.style,
                with_cp=with_cp,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                dcn=dcn,
                gcb=gcb,
                gen_attention=gen_attention,
                gen_attention_blocks=stage_with_gen_attention[i])
            self.inplanes = planes * self.block.expansion
            layer_name = 'layer{}'.format(i + 1)
            self.add_module(layer_name, res_layer)
            self.res_layers.append(layer_name)

        self._freeze_stages()

        p = min( 2**(len(self.stage_blocks) - 1), self.max_planes)
        self.feat_dim = self.block.expansion * self.basic_planes * p

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    def _make_stem_layer(self, in_channels):
        kernel = (3,3,5)
        kernel = (5,5,5)
        padding = get_padding_same_featsize(kernel)
        self.conv1 = build_conv_layer(
            self.s3d_conv_cfg,
            in_channels,
            64,
            kernel_size=kernel,
            stride=2,
            padding=padding,
            bias=False)
        self.norm1_name, norm1 = build_norm_layer(self.s3d_norm_cfg, 64, postfix=1)
        self.add_module(self.norm1_name, norm1)
        self.relu = ME.MinkowskiReLU(inplace=True)
        if self.stem_stride == 4:
          self.maxpool = mink_max_pool(kernel_size=3, stride=2, padding=1)
        elif self.stem_stride == 2:
          self.maxpool = mink_max_pool(kernel_size=3, stride=(1,1,2), padding=(0,0,1))
        pass

    def _make_project_layers(self):
        self.prj_block = BasicBlock
        self.prj_stage_blocks = (3,)*5

        z_strides = (2,) * 5
        kernels = ( (3,3,3), (3,3,3), (3,3,3), (3,3,3), (3,3,3) )
        dilations = (1,1,1,1,1)
        inplanes = 64
        self.prj_planes = [self.basic_planes * r for r in (2,4,4,4,4)]

        assert np.product(z_strides) * 4 >= self.max_zdim
        self.prj_layers = []
        for i, num_blocks in enumerate(self.prj_stage_blocks):
            z_stride = z_strides[i]
            dilation = dilations[i]
            dcn =  None
            gcb =  None
            planes = self.prj_planes[i]
            prj_layer = make_res_layer(
                self.prj_block,
                inplanes,
                planes,
                num_blocks,
                kernel = kernels[i],
                stride=(1,1,z_stride),
                dilation=dilation,
                style=self.style,
                with_cp=self.with_cp,
                conv_cfg=self.s3d_conv_cfg,
                norm_cfg=self.s3d_norm_cfg,
                dcn=dcn,
                gcb=gcb,
                gen_attention=self.gen_attention,
                gen_attention_blocks=())
            inplanes = planes * self.prj_block.expansion
            layer_name = 'prj_layer{}'.format(i + 1)
            self.add_module(layer_name, prj_layer)
            self.prj_layers.append(layer_name)
            #print(prj_layer)
            pass
        pass

    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            self.norm1.eval()
            for m in [self.conv1, self.norm1]:
                for param in m.parameters():
                    param.requires_grad = False

        for i in range(1, self.frozen_stages + 1):
            m = getattr(self, 'layer{}'.format(i))
            m.eval()
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

    def forward(self, x, gt_bboxes=None):
        if SHOW_NET:
          _show_sparse_ls_shapes([x], 'res in')
        if RECORD_T:
          t0 = time.time()
        bev = self.project(x)
        x = bev
        if RECORD_T:
          t1 = time.time()

        if SHOW_NET:
          _show_tensor_ls_shapes([x], 'bev in')
        outs = []
        for i, layer_name in enumerate(self.res_layers):
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if SHOW_NET:
              _show_tensor_ls_shapes([x], f'res {i}')
            if i in self.out_indices:
                outs.append(x)
        if RECORD_T:
          t2 = time.time()
          print(f'\tresnet forward. proj:{t1-t0:.3f} reslayers:{t2-t1:.3f}')

        if SHOW_NET:
          _show_tensor_ls_shapes(outs, 'res out')

        if DEBUG_CFG.VISUAL_RESNET_FEAT_OUT:
          for i in range(len(outs)):
            visual_utils._show_feats(outs[i], gt_bboxes, self.stem_stride * 2**(i))
          import pdb; pdb.set_trace()  # XXX BREAKPOINT
          pass
        return tuple(outs)

    def project(self, x):
        in_shape = (x.C.max(0)[0]+1)[[2,1]]
        if RECORD_T:
          t0 = time.time()
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.relu(x)
        if RECORD_T:
          t1 = time.time()
        x = self.maxpool(x)
        if SHOW_NET:
          debug_utils._show_sparse_ls_shapes([x], 'stem')
        if RECORD_T:
          t2 = time.time()

        for i, layer_name in enumerate(self.prj_layers):
            proj_layer = getattr(self, layer_name)
            x = proj_layer(x)
            if SHOW_NET:
              debug_utils._show_sparse_ls_shapes([x], f'prj {i}')
        bev_dense, bev_min, bev_stride = x.dense()
        x = bev_dense.max(-1)[0]
        x = x.permute(0,1,3,2)

        stem_shape = x.shape[2:]
        shape_err = in_shape/self.stem_stride - torch.Tensor([*stem_shape])
        assert torch.abs(shape_err).max() < 4, f'stemp stride may be incorrect'

        if self.bev_pad_pixels != 0:
          p = int(np.ceil(self.bev_pad_pixels / self.stem_stride))
          x = nn.functional.pad(x, (p,p,p,p), mode='constant', value=0)
          pass
        if RECORD_T:
          t3 = time.time()
          print(f'\tresnet forward. conv1:{t1-t0:.3f} max: {t2-t1:.3f} prjlayers:{t3-t2:.3f}')
        return x

    def train(self, mode=True):
        super(S3dProj_BevResNet, self).train(mode)
        self._freeze_stages()
        if mode and self.norm_eval:
            for m in self.modules():
                # trick: eval have effect on BatchNorm only
                if isinstance(m, _BatchNorm):
                    m.eval()

