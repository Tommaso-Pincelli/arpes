"""
Helper functions for coordinate transformations. All the functions here
assume standard polar angles, for better or worse, so you might need to
massage your inputs slightly in order to get them into an appropriate form.

Functions here must accept constants or numpy arrays as valid inputs,
so all standard math functions have been replaced by their equivalents out
of numpy. Array broadcasting should handle any issues or weirdnesses that
would encourage the use of direct iteration, but in case you need to write
a conversion directly, be aware that any functions here must work on arrays
as well for consistency with client code.

Through the code that follows, there are some conventions on the names of angles
to make the code easier to follow:

Standard vertically oriented cryostats:

'polar' is the name of the angle that describes rotation around \hat{z}
'beta' is the name of the angle that describes rotation around \hat{x}
'sample_phi' is the name of the angle that describes rotation around the sample normal
'phi' is the name of the angle that describes the angle along the analyzer entrance slit

Additionally, everywhere, 'eV' denotes binding energies. Other energy units should
be labelled as:

Kinetic energy -> 'kinetic_energy'
Binding energy -> 'binding_energy'
Photon energy -> 'hv'

Other angles:
Sample elevation/tilt/beta angle -> 'polar'
Analyzer polar angle -> 'phi'
"""

# pylint: disable=W0613, C0103

import collections
import warnings
from copy import deepcopy

import numpy as np
import scipy.interpolate
import xarray as xr

from arpes.provenance import provenance, update_provenance
from .kx_ky_conversion import *
from .kz_conversion import *

__all__ = ['convert_to_kspace', 'slice_along_path']

# TODO Add conversion utilities that work for lower dimensionality, i.e. the ToF
# TODO Check if conversion utilities work for constant energy cuts


def infer_kspace_coordinate_transform(arr: xr.DataArray):
    """
    Infers appropriate coordinate transform for arr to momentum space.

    This takes into account the extra metadata attached to arr that might be
    useful in inferring the requirements of the coordinate transform, like the
    orientation of the spectrometer slit, and other experimental concerns
    :param arr:
    :return: dict with keys ``target_coordinates``, and a map of the appropriate
    conversion functions
    """
    old_coords = deepcopy(list(arr.coords))
    assert ('eV' in old_coords)
    old_coords.remove('eV')
    old_coords.sort()

    new_coords = {
        ('phi',): ['kp'],
        ('phi', 'polar',): ['kx', 'ky'],
        ('hv', 'phi',): ['kp', 'kz'],
        ('hv', 'phi', 'polar',): ['kx', 'ky', 'kz'],
    }.get(tuple(old_coords))

    # At this point we need to do a bit of work in order to determine the functions
    # that interpolate from k-space back into the recorded variable space

    # TODO Also provide the Jacobian of the coordinate transform to properly
    return {
        'dims': new_coords,
        'transforms': {

        },
        'calculate_bounds': None,
        'jacobian': None,
    }


def grid_interpolator_from_dataarray(arr: xr.DataArray, fill_value=0.0, method='linear',
                                     bounds_error=False):
    """
    Translates the contents of an xarray.DataArray into a scipy.interpolate.RegularGridInterpolator.

    This is principally used for coordinate translations.
    """
    flip_axes = set()
    for d in arr.dims:
        c = arr.coords[d]
        if len(c) > 1 and c[1] - c[0] < 0:
            flip_axes.add(d)

    values = arr.values
    for dim in flip_axes:
        values = np.flip(values, arr.dims.index(dim))

    return scipy.interpolate.RegularGridInterpolator(
        points=[arr.coords[d].values[::-1] if d in flip_axes else arr.coords[d].values for d in arr.dims],
        values=values,
        bounds_error=bounds_error, fill_value=fill_value, method=method)


def slice_along_path(arr: xr.DataArray, interpolation_points=None, axis_name=None, resolution=None,
                     shift_gamma=True, **kwargs):
    """
    Interpolates along a path through a volume. If the volume is higher dimensional than the desired path, the
    interpolation is broadcasted along the free dimensions. This allows one to specify a k-space path and receive
    the band structure along this path in k-space.

    Points can either by specified by coordinates, or by reference to symmetry points, should they exist in the source
    array. These symmetry points are translated to regular coordinates immediately, but are provided as a convenience.
    If not all points specify the same set of coordinates, an attempt will be made to unify the coordinates. As an example,
    if the specified path is (kx=0, ky=0, T=20) -> (kx=1, ky=1), the path will be made between (kx=0, ky=0, T=20) ->
    (kx=1, ky=1, T=20). On the other hand, the path (kx=0, ky=0, T=20) -> (kx=1, ky=1, T=40) -> (kx=0, ky=1) will result
    in an error because there is no way to break the ambiguity on the temperature for the last coordinate.

    A reasonable value will be chosen for the resolution, near the maximum resolution of any of the interpolated
    axes by default.

    This function transparently handles the entire path. An alternate approach would be to convert each segment
    separately and concatenate the interpolated axis with xarray.

    If the sentinel value 'G' for the Gamma point is included in the interpolation points, the coordinate axis of the
    interpolated coordinate will be shifted so that its value at the Gamma point is 0. You can opt out of this with the
    parameter 'shift_gamma'

    :param arr: Source data
    :param interpolation_points: Path vertices
    :param axis_name: Label for the interpolated axis. Under special circumstances a reasonable name will be chosen,
    such as when the interpolation dimensions are kx and ky: in this case the interpolated dimension will be labeled kp.
    In mixed or ambiguous situations the axis will be labeled by the default value 'inter'.
    :param resolution: Requested resolution along the interpolated axis.
    :param shift_gamma: Controls whether the interpolated axis is shifted to a value of 0 at Gamma.
    :param kwargs:
    :return: xr.DataArray containing the interpolated data.
    """

    if interpolation_points is None:
        raise ValueError('You must provide points specifying an interpolation path')

    parsed_interpolation_points = [
        x if isinstance(x, collections.Iterable) and not isinstance(x, str) else arr.attrs['symmetry_points'][x]
        for x in interpolation_points
    ]

    free_coordinates = list(arr.dims)
    seen_coordinates = collections.defaultdict(set)
    for point in parsed_interpolation_points:
        for coord, value in point.items():
            seen_coordinates[coord].add(value)
            if coord in free_coordinates:
                free_coordinates.remove(coord)

    for point in parsed_interpolation_points:
        for coord, values in seen_coordinates.items():
            if coord not in point:
                if len(values) != 1:
                    raise ValueError('Ambiguous interpolation waypoint broadcast at dimension {}'.format(coord))
                else:
                    point[coord] = list(values)[0]

    if axis_name is None:
        axis_name = {
            ('phi', 'polar',): 'angle',
            ('kx', 'ky',): 'kp',
            ('kx', 'kz',): 'k',
            ('ky', 'kz',): 'k',
            ('kx', 'ky', 'kz',): 'k'
        }.get(tuple(sorted(seen_coordinates.keys())), 'inter')

        if axis_name == 'angle' or axis_name == 'inter':
            warnings.warn('Interpolating along axes with different dimensions '
                          'will not include Jacobian correction factor.')

    converted_coordinates = None
    converted_dims = free_coordinates + [axis_name]

    path_segments = list(zip(parsed_interpolation_points, parsed_interpolation_points[1:]))

    def element_distance(waypoint_a, waypoint_b):
        delta = np.array([waypoint_a[k] - waypoint_b[k] for k in waypoint_a.keys()])
        return np.linalg.norm(delta)

    def required_sampling_density(waypoint_a, waypoint_b):
        ks = waypoint_a.keys()
        dist = element_distance(waypoint_a, waypoint_b)
        delta = np.array([waypoint_a[k] - waypoint_b[k] for k in ks])
        delta_idx = [abs(d / (arr.coords[k][1] - arr.coords[k][0])) for d, k in zip(delta, ks)]
        return dist / np.max(delta_idx)

    # Approximate how many points we should use
    segment_lengths = [element_distance(*segment) for segment in path_segments]
    path_length = sum(segment_lengths)

    gamma_offset = 0 # offset the gamma point to a k coordinate of 0 if possible
    if 'G' in interpolation_points and shift_gamma:
        gamma_offset = sum(segment_lengths[0:interpolation_points.index('G')])

    if resolution is None:
        resolution = np.min([required_sampling_density(*segment) for segment in path_segments])

    def converter_for_coordinate_name(name):
        def raw_interpolator(*coordinates):
            return coordinates[free_coordinates.index(name)]

        if name in free_coordinates:
            return raw_interpolator

        # Conversion involves the interpolated coordinates
        def interpolated_coordinate_to_raw(*coordinates):
            # Coordinate order is [*free_coordinates, interpolated]
            interpolated = coordinates[len(free_coordinates)] + gamma_offset

            # Start with empty array that we will mask writes onto
            # We need to go with a masking approach rather than a concatenation based one because the coordinates
            # come from np.meshgrid
            dest_coordinate = np.zeros(shape=interpolated.shape)

            start = 0
            for i, l in enumerate(segment_lengths):
                end = start + l
                normalized = (interpolated - start) / l
                seg_start, seg_end = path_segments[i]
                dim_start, dim_end = seg_start[name], seg_end[name]
                mask = np.logical_and(normalized >= 0, normalized < 1)
                dest_coordinate[mask] = \
                    dim_start * (1 - normalized[mask]) + dim_end * normalized[mask]
                start = end

            return dest_coordinate

        return interpolated_coordinate_to_raw

    converted_coordinates = {d: arr.coords[d].values for d in free_coordinates}

    # Adjust this coordinate under special circumstances
    converted_coordinates[axis_name] = np.linspace(0, path_length, int(path_length / resolution)) - gamma_offset

    converted_arr = convert_coordinates(
        arr,
        converted_coordinates,
        {
            'dims': converted_dims,
            'transforms': dict(zip(arr.dims, [converter_for_coordinate_name(d) for d in arr.dims]))
        }
    )

    del converted_arr.attrs['id']
    provenance(converted_arr, arr, {
        'what': 'Slice along path',
        'by': 'slice_along_path',
        'parsed_interpolation_points': parsed_interpolation_points,
        'interpolation_points': interpolation_points,
    })

    return converted_arr


@update_provenance('Automatically k-space converted')
def convert_to_kspace(arr: xr.DataArray, resolution=None, **kwargs):
    # TODO be smarter about the resolution inference
    old_dims = list(deepcopy(arr.dims))
    remove_dims = ['eV', 'delay', 'cycle', 'T']
    removed = []
    for to_remove in remove_dims:
        if to_remove in old_dims:
            removed.append(to_remove)
            old_dims.remove(to_remove)

    if 'eV' in removed:
        removed.remove('eV') # This is put at the front as a standardization

    old_dims.sort()

    if len(old_dims) == 0:
        # Was a core level scan or something similar
        return arr

    converted_dims = ['eV'] + {
        ('phi',): ['kp'],
        ('phi', 'polar'): ['kx', 'ky'],
        ('hv', 'phi'): ['kp', 'kz'],
        ('hv', 'phi', 'polar'): ['kx', 'ky', 'kz'],
    }.get(tuple(old_dims)) + removed

    convert_cls = {
        ('phi',): ConvertKp,
        ('phi', 'polar'): ConvertKxKy,
        ('hv', 'phi'): ConvertKpKz,
    }.get(tuple(old_dims))
    converter = convert_cls(arr, converted_dims)
    converted_coordinates = converter.get_coordinates(resolution)

    return convert_coordinates(
        arr, converted_coordinates, {
            'dims': converted_dims,
            'transforms': dict(zip(arr.dims, [converter.conversion_for(d) for d in arr.dims]))})


def convert_coordinates(arr: xr.DataArray, target_coordinates, coordinate_transform):
    ordered_source_dimensions = arr.dims
    grid_interpolator = grid_interpolator_from_dataarray(
        arr.transpose(*ordered_source_dimensions), fill_value=float('nan'))

    # Skip the Jacobian correction for now
    # Convert the raw coordinate axes to a set of gridded points
    meshed_coordinates = np.meshgrid(*[target_coordinates[dim] for dim in coordinate_transform['dims']],
                                     indexing='ij')
    meshed_coordinates = [meshed_coord.ravel() for meshed_coord in meshed_coordinates]

    ordered_transformations = [coordinate_transform['transforms'][dim] for dim in arr.dims]
    converted_volume = grid_interpolator(np.array([tr(*meshed_coordinates) for tr in ordered_transformations]).T)

    # Wrap it all up
    return xr.DataArray(
        np.reshape(converted_volume, [len(target_coordinates[d]) for d in coordinate_transform['dims']], order='C'),
        target_coordinates,
        coordinate_transform['dims'],
        attrs=arr.attrs
    )