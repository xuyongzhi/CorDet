import random

import logging
import numpy as np
import scipy
import scipy.ndimage
import scipy.interpolate
import torch

import MinkowskiEngine as ME
#from MinkowskiEngine import SparseTensor


# A sparse tensor consists of coordinates and associated features.
# You must apply augmentation to both.
# In 2D, flip, shear, scale, and rotation of images are coordinate transformation
# color jitter, hue, etc., are feature transformations
##############################
# Feature transformations
##############################
class ChromaticTranslation(object):
  """Add random color to the image, input must be an array in [0,255] or a PIL image"""

  def __init__(self, trans_range_ratio=1e-1):
    """
    trans_range_ratio: ratio of translation i.e. 255 * 2 * ratio * rand(-0.5, 0.5)
    """
    self.trans_range_ratio = trans_range_ratio

  def __call__(self, coords, feats, labels, gt_bboxes=None, img_meta=None):
    if random.random() < 0.95:
      tr = (np.random.rand(1, 3) - 0.5) * 255 * 2 * self.trans_range_ratio
      feats[:, :3] = np.clip(tr + feats[:, :3], 0, 255)
    return coords, feats, labels, gt_bboxes, img_meta


class ChromaticAutoContrast(object):

  def __init__(self, randomize_blend_factor=True, blend_factor=0.5):
    self.randomize_blend_factor = randomize_blend_factor
    self.blend_factor = blend_factor

  def __call__(self, coords, feats, labels, gt_bboxes=None, img_meta=None):
    if random.random() < 0.2:
      # mean = np.mean(feats, 0, keepdims=True)
      # std = np.std(feats, 0, keepdims=True)
      # lo = mean - std
      # hi = mean + std
      lo = feats[:, :3].min(0, keepdims=True)
      hi = feats[:, :3].max(0, keepdims=True)
      assert hi.max() > 1, f"invalid color value. Color is supposed to be [0-255]"

      scale = 255 / (hi - lo)

      contrast_feats = (feats[:, :3] - lo) * scale

      blend_factor = random.random() if self.randomize_blend_factor else self.blend_factor
      feats[:, :3] = (1 - blend_factor) * feats[:,:3] + blend_factor * contrast_feats
    return coords, feats, labels, gt_bboxes, img_meta


class ChromaticJitter(object):

  def __init__(self, std=0.01):
    self.std = std

  def __call__(self, coords, feats, labels, gt_bboxes=None, img_meta=None):
    if random.random() < 0.95:
      noise = np.random.randn(feats.shape[0], 3)
      noise *= self.std * 255
      feats[:, :3] = np.clip(noise + feats[:, :3], 0, 255)
    return coords, feats, labels, gt_bboxes, img_meta


class HueSaturationTranslation(object):

  @staticmethod
  def rgb_to_hsv(rgb):
    # Translated from source of colorsys.rgb_to_hsv
    # r,g,b should be a numpy arrays with values between 0 and 255
    # rgb_to_hsv returns an array of floats between 0.0 and 1.0.
    rgb = rgb.astype('float')
    hsv = np.zeros_like(rgb)
    # in case an RGBA array was passed, just copy the A channel
    hsv[..., 3:] = rgb[..., 3:]
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.max(rgb[..., :3], axis=-1)
    minc = np.min(rgb[..., :3], axis=-1)
    hsv[..., 2] = maxc
    mask = maxc != minc
    hsv[mask, 1] = (maxc - minc)[mask] / maxc[mask]
    rc = np.zeros_like(r)
    gc = np.zeros_like(g)
    bc = np.zeros_like(b)
    rc[mask] = (maxc - r)[mask] / (maxc - minc)[mask]
    gc[mask] = (maxc - g)[mask] / (maxc - minc)[mask]
    bc[mask] = (maxc - b)[mask] / (maxc - minc)[mask]
    hsv[..., 0] = np.select([r == maxc, g == maxc], [bc - gc, 2.0 + rc - bc], default=4.0 + gc - rc)
    hsv[..., 0] = (hsv[..., 0] / 6.0) % 1.0
    return hsv

  @staticmethod
  def hsv_to_rgb(hsv):
    # Translated from source of colorsys.hsv_to_rgb
    # h,s should be a numpy arrays with values between 0.0 and 1.0
    # v should be a numpy array with values between 0.0 and 255.0
    # hsv_to_rgb returns an array of uints between 0 and 255.
    rgb = np.empty_like(hsv)
    rgb[..., 3:] = hsv[..., 3:]
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    i = (h * 6.0).astype('uint8')
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    conditions = [s == 0.0, i == 1, i == 2, i == 3, i == 4, i == 5]
    rgb[..., 0] = np.select(conditions, [v, q, p, p, t, v], default=v)
    rgb[..., 1] = np.select(conditions, [v, v, v, q, p, p], default=t)
    rgb[..., 2] = np.select(conditions, [v, p, t, v, v, q], default=p)
    return rgb.astype('uint8')

  def __init__(self, hue_max, saturation_max):
    self.hue_max = hue_max
    self.saturation_max = saturation_max

  def __call__(self, coords, feats, labels):
    # Assume feat[:, :3] is rgb
    hsv = HueSaturationTranslation.rgb_to_hsv(feats[:, :3])
    hue_val = (random.random() - 0.5) * 2 * self.hue_max
    sat_ratio = 1 + (random.random() - 0.5) * 2 * self.saturation_max
    hsv[..., 0] = np.remainder(hue_val + hsv[..., 0] + 1, 1)
    hsv[..., 1] = np.clip(sat_ratio * hsv[..., 1], 0, 1)
    feats[:, :3] = np.clip(HueSaturationTranslation.hsv_to_rgb(hsv), 0, 255)

    return coords, feats, labels


##############################
# Coordinate transformations
##############################
class RandomDropout(object):

  def __init__(self, dropout_ratio=0.2, dropout_application_ratio=0.5):
    """
    upright_axis: axis index among x,y,z, i.e. 2 for z
    """
    self.dropout_ratio = dropout_ratio
    self.dropout_application_ratio = dropout_application_ratio

  def __call__(self, coords, feats, labels, gt_bboxes=None, img_meta=None):
    if random.random() < self.dropout_ratio:
      N = len(coords)
      inds = np.random.choice(N, int(N * (1 - self.dropout_ratio)), replace=False)
      if img_meta is not None:
        img_meta['data_aug']['dropout_ratio'] = (self.dropout_ratio, True)
      return coords[inds], feats[inds], labels[inds], gt_bboxes, img_meta
    return coords, feats, labels, gt_bboxes, img_meta


class RandomHorizontalFlip(object):

  def __init__(self, upright_axis, is_temporal):
    """
    upright_axis: axis index among x,y,z, i.e. 2 for z
    """
    self.is_temporal = is_temporal
    self.D = 4 if is_temporal else 3
    self.upright_axis = {'x': 0, 'y': 1, 'z': 2}[upright_axis.lower()]
    # Use the rest of axes for flipping.
    self.horz_axes = set(range(self.D)) - set([self.upright_axis])

  def __call__(self, coords, feats, labels, gt_bboxes=None, img_meta=None):
    if random.random() < 1.95:
      if img_meta is not None:
        img_meta['data_aug']['flip'] = {'x':-1, 'y':-1}
      for curr_ax in self.horz_axes:
        if random.random() < 0.5:
          coord_max = np.max(coords[:, curr_ax])
          coords[:, curr_ax] = coord_max - coords[:, curr_ax]

          # flip surface normal
          feats[:,3:6][:,curr_ax] *= -1

          if img_meta is not None:
            gt_bboxes = bboxes_flip_scope_itl(gt_bboxes, curr_ax, coord_max, img_meta['obj_rep'])
            img_meta['data_aug']['flip'][ ['x','y'][curr_ax] ] = coord_max
    return coords, feats, labels, gt_bboxes, img_meta

def bboxes_flip_scope_itl(bboxes_in, curr_ax, coord_max, obj_rep):
  from obj_geo_utils.obj_utils import OBJ_REPS_PARSE

  if obj_rep == 'XYXYSin2':
    lines0 = bboxes_in
    assert lines0.shape[1] == 5
    assert curr_ax==0 or curr_ax==1
    tmp = coord_max - lines0[:, curr_ax]
    lines0[:, curr_ax] = coord_max - lines0[:, curr_ax + 2]
    lines0[:, curr_ax + 2] = tmp
    lines0[:,4] = -lines0[:,4]
    return lines0
  elif obj_rep == 'Rect4CornersZ0Z1':
    assert bboxes_in.shape[1] == 10
    assert curr_ax==0 or curr_ax==1
    flipped = bboxes_in.copy()
    if curr_ax == 0:
      flipped[..., 0:8:2] = coord_max - bboxes_in[..., 0:8:2]
    if curr_ax == 1:
      flipped[..., 1:9:2] = coord_max - bboxes_in[..., 1:9:2]
    flipped = OBJ_REPS_PARSE.update_corners_order( flipped, obj_rep )
    return flipped
  else:
    raise NotImplementedError

class ElasticDistortion:

  def __init__(self, distortion_params):
    self.distortion_params = distortion_params

  def elastic_distortion(self, coords, feats, labels, granularity, magnitude):
    """Apply elastic distortion on sparse coordinate space.

      pointcloud: numpy array of (number of points, at least 3 spatial dims)
      granularity: size of the noise grid (in same scale[m/cm] as the voxel grid)
      magnitude: noise multiplier
    """
    blurx = np.ones((3, 1, 1, 1)).astype('float32') / 3
    blury = np.ones((1, 3, 1, 1)).astype('float32') / 3
    blurz = np.ones((1, 1, 3, 1)).astype('float32') / 3
    coords_min = coords.min(0)

    # Create Gaussian noise tensor of the size given by granularity.
    noise_dim = ((coords - coords_min).max(0) // granularity).astype(int) + 3
    noise = np.random.randn(*noise_dim, 3).astype(np.float32)

    # Smoothing.
    for _ in range(2):
      noise = scipy.ndimage.filters.convolve(noise, blurx, mode='constant', cval=0)
      noise = scipy.ndimage.filters.convolve(noise, blury, mode='constant', cval=0)
      noise = scipy.ndimage.filters.convolve(noise, blurz, mode='constant', cval=0)

    # Trilinear interpolate noise filters for each spatial dimensions.
    ax = [
        np.linspace(d_min, d_max, d)
        for d_min, d_max, d in zip(coords_min - granularity, coords_min + granularity *
                                   (noise_dim - 2), noise_dim)
    ]
    interp = scipy.interpolate.RegularGridInterpolator(ax, noise, bounds_error=0, fill_value=0)
    coords += interp(coords) * magnitude
    return coords, feats, labels

  def __call__(self, coords, feats, labels):
    if self.distortion_params is not None:
      if random.random() < 0.95:
        for granularity, magnitude in self.distortion_params:
          coords, feats, labels = self.elastic_distortion(coords, feats, labels, granularity,
                                                          magnitude)
    return coords, feats, labels


class Compose(object):
  """Composes several transforms together."""

  def __init__(self, transforms):
    self.transforms = transforms

  def __call__(self, *args):
    for t in self.transforms:
      args = t(*args)
    return args


class cfl_collate_fn_factory:
  """Generates collate function for coords, feats, labels.

    Args:
      limit_numpoints: If 0 or False, does not alter batch size. If positive integer, limits batch
                       size so that the number of input coordinates is below limit_numpoints.
  """

  def __init__(self, limit_numpoints):
    self.limit_numpoints = limit_numpoints

  def __call__(self, list_data):
    coords, feats, p_labels, img_infos = list(zip(*list_data))
    coords_batch, feats_batch, p_labels_batch = [], [], []

    batch_id = 0
    batch_num_points = 0
    for batch_id, _ in enumerate(coords):
      num_points = coords[batch_id].shape[0]
      batch_num_points += num_points
      if self.limit_numpoints and batch_num_points > self.limit_numpoints:
        num_full_points = sum(len(c) for c in coords)
        num_full_batch_size = len(coords)
        logging.warning(
            f'\t\tCannot fit {num_full_points} points into {self.limit_numpoints} points '
            f'limit. Truncating batch size at {batch_id} out of {num_full_batch_size} with {batch_num_points - num_points}.'
        )
        break
      coords_batch.append(torch.from_numpy(coords[batch_id]).int())
      feats_batch.append(torch.from_numpy(feats[batch_id]))
      #p_labels_batch.append(torch.from_numpy(p_labels[batch_id]).int())

      batch_id += 1

    # Concatenate all lists
    coords_batch, feats_batch = ME.utils.sparse_collate(coords_batch, feats_batch)
    feats_batch = feats_batch.float()

    img_metas = [ m['img_meta'] for m in img_infos]

    data = dict(img = (coords_batch, feats_batch),
                img_meta = img_metas,
                )
    if 'gt_bboxes' in img_infos[0]:
      gt_bboxes = [torch.from_numpy( m['gt_bboxes'] ) for m in img_infos]
      gt_labels = [torch.from_numpy( m['gt_labels'] ) for m in img_infos]
      data['gt_bboxes'] = gt_bboxes
      data['gt_labels'] = gt_labels
    return data
    #return coords_batch, feats_batch.float(), p_labels_batch


class cflt_collate_fn_factory:
  """Generates collate function for coords, feats, labels, point_clouds, transformations.

    Args:
      limit_numpoints: If 0 or False, does not alter batch size. If positive integer, limits batch
                       size so that the number of input coordinates is below limit_numpoints.
  """

  def __init__(self, limit_numpoints):
    self.limit_numpoints = limit_numpoints

  def __call__(self, list_data):
    coords, feats, labels, transformations = list(zip(*list_data))
    cfl_collate_fn = cfl_collate_fn_factory(limit_numpoints=self.limit_numpoints)
    coords_batch, feats_batch, labels_batch = cfl_collate_fn(list(zip(coords, feats, labels)))
    num_truncated_batch = coords_batch[:, -1].max().item() + 1

    batch_id = 0
    transformations_batch = []
    for transformation in transformations:
      if batch_id >= num_truncated_batch:
        break
      transformations_batch.append(torch.from_numpy(transformation).float())
      batch_id += 1

    return coords_batch, feats_batch, labels_batch, transformations_batch