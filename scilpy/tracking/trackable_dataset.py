# -*- coding: utf-8 -*-
import numpy as np
from dipy.core.interpolation import trilinear_interpolate4d, \
    nearestneighbor_interpolate


class Dataset(object):
    """
    Class to access/interpolate data from nibabel object
    """

    def __init__(self, img, interpolation=None):
        """
        Parameters
        ----------
        img: nibabel image
            The nibabel image from which to get the data
        interpolation: str or None
            The interpolation choice amongst "trilinear" or "nearest". If
            None, functions getting a coordinate in mm instead of voxel
            coordinates are not available.
        """
        self.interpolation = interpolation
        if self.interpolation:
            if not (self.interpolation == 'trilinear' or
                    self.interpolation == 'nearest'):
                raise Exception("Interpolation must be 'trilinear' or "
                                "'nearest'")

        self.pixdim = img.header.get_zooms()[:3]
        self.data = img.get_fdata(caching='unchanged', dtype=np.float64)

        # Expand dimensionality to support uniform 4d interpolation
        if self.data.ndim == 3:
            self.data = np.expand_dims(self.data, axis=3)

        self.dim = self.data.shape[0:4]
        self.nbr_voxel = self.data.size

    def get_voxel_value(self, i, j, k):
        """
        Get the voxel value at x, y, z in the dataset
        if the coordinates are out of bound, the nearest voxel
        value is taken.

        Parameters
        ----------
        i, j, k: ints
            Voxel indice along each axis.

        Return
        ------
        value: ndarray (self.dim[-1],)
            The value evaluated at voxel x, y, z.
        """
        if not self.is_voxel_in_bound(i, j, k):
            i = max(0, min(self.dim[0] - 1, i))
            j = max(0, min(self.dim[1] - 1, j))
            k = max(0, min(self.dim[2] - 1, k))

        return self.data[i][j][k]

    def is_voxel_in_bound(self, i, j, k):
        """
        Test if voxel is in dataset range.

        Parameters
        ----------
        i, j, k: ints
            Voxel indice along each axis.

        Return
        ------
        out: bool
            True if voxel is in dataset range, False otherwise.
        """
        return (0 <= i < self.dim[0] and 0 <= j < self.dim[1] and
                0 <= k < self.dim[2])

    def get_voxel_at_position(self, x, y, z):
        """
        Get the 3D indice of the closest voxel at position x, y, z expressed
        in mm.

        Parameters
        ----------
        x, y, z: floats
            Position coordinate (mm) along x, y, z axis.

        Return
        ------
        out: list
            3D indice of voxel at position x, y, z.
        """
        return [(x + self.pixdim[0] / 2) // self.pixdim[0],
                (y + self.pixdim[1] / 2) // self.pixdim[1],
                (z + self.pixdim[2] / 2) // self.pixdim[2]]

    def get_voxel_coordinate(self, x, y, z):
        """
        Get voxel space coordinates at position x, y, z (mm).

        Parameters
        ----------
        x, y, z: floats
            Position coordinate (mm) along x, y, z axis.

        Return
        ------
        out: list
            Voxel space coordinates for position x, y, z.
        """
        return [x / self.pixdim[0], y / self.pixdim[1], z / self.pixdim[2]]

    def get_voxel_value_at_position(self, x, y, z):
        """
        Get value of the voxel closest to position x, y, z (mm) in the dataset.
        No interpolation is done.

        Parameters
        ----------
        x, y, z: floats
            Position coordinate (mm) along x, y, z axis.

        Return
        ------
        value: ndarray (self.dim[-1],)
            The value evaluated at position x, y, z.
        """
        return self.get_voxel_value(*self.get_voxel_at_position(x, y, z))

    def get_position_value(self, x, y, z):
        """
        Get the voxel value at voxel position x, y, z (mm) in the dataset.
        If the coordinates are out of bound, the nearest voxel value is taken.
        Value is interpolated based on the value of self.interpolation.

        Parameters
        ----------
        x, y, z: floats
            Position coordinate (mm) along x, y, z axis.

        Return
        ------
        value: ndarray (self.dims[-1],) or float
            Interpolated value at position x, y, z (mm). If the last dimension
            is of length 1, return a scalar value.
        """
        if self.interpolation is not None:
            if not self.is_position_in_bound(x, y, z):
                eps = float(1e-8)  # Epsilon to exclude upper borders
                x = max(-self.pixdim[0] / 2,
                        min(self.pixdim[0] * (self.dim[0] - 0.5 - eps), x))
                y = max(-self.pixdim[1] / 2,
                        min(self.pixdim[1] * (self.dim[1] - 0.5 - eps), y))
                z = max(-self.pixdim[2] / 2,
                        min(self.pixdim[2] * (self.dim[2] - 0.5 - eps), z))
            coord = np.array(self.get_voxel_coordinate(x, y, z),
                             dtype=np.float64)

            if self.interpolation == 'nearest':
                result = nearestneighbor_interpolate(self.data, coord)
            else:
                # Trilinear
                result = trilinear_interpolate4d(self.data, coord)

            # Squeezing returns only value instead of array of length 1 if 3D
            # data
            return np.squeeze(result)

        else:
            raise Exception("No interpolation method was given, cannot run "
                            "this method..")

    def is_position_in_bound(self, x, y, z):
        """
        Test if the position x, y, z mm is in the dataset range.

        Parameters
        ----------
        x, y, z: floats
            Position coordinate (mm) along x, y, z axis.

        Return
        ------
        value: bool
            True if position is in dataset range and false otherwise.
        """
        return self.is_voxel_in_bound(*self.get_voxel_at_position(x, y, z))


class SeedGenerator(Dataset):
    """
    Class to get seeding positions
    """

    def __init__(self, img):
        super(SeedGenerator, self).__init__(img, None)
        self.seeds = np.array(np.where(np.squeeze(self.data) > 0)).transpose()

    def get_next_pos(self, random_generator, indices, which_seed):
        """
        Generate the next seed position.

        Parameters
        ----------
        random_generator : numpy random generator
            Initialized numpy number generator.
        indices : List
            Indices of current seeding map.
        which_seed : int
            Seed number to be processed.

        Return
        ------
        seed_pos: tuple
            Position of next seed expressed in mm.
        """
        len_seeds = len(self.seeds)
        if len_seeds == 0:
            return []

        half_voxel_range = [self.pixdim[0] / 2,
                            self.pixdim[1] / 2,
                            self.pixdim[2] / 2]

        # Voxel selection from the seeding mask
        ind = which_seed % len_seeds
        x, y, z = self.seeds[indices[np.asscalar(ind)]]

        # Subvoxel initial positioning
        r_x = random_generator.uniform(-half_voxel_range[0],
                                       half_voxel_range[0])
        r_y = random_generator.uniform(-half_voxel_range[1],
                                       half_voxel_range[1])
        r_z = random_generator.uniform(-half_voxel_range[2],
                                       half_voxel_range[2])

        return x * self.pixdim[0] + r_x, y * self.pixdim[1] \
            + r_y, z * self.pixdim[2] + r_z

    def init_pos(self, random_initial_value, first_seed_of_chunk):
        """
        Initialize numpy number generator according to user's parameter
        and indexes from the seeding map.

        Parameters
        ----------
        random_initial_value : int
            The "seed" for the random generator.
        first_seed_of_chunk : int
            Number of seeds to skip (skip parameter + multi-processor skip).

        Return
        ------
        random_generator : numpy random generator
            Initialized numpy number generator.
        indices : List
            Indices of current seeding map.
        """
        random_generator = np.random.RandomState(random_initial_value)
        indices = np.arange(len(self.seeds))
        random_generator.shuffle(indices)

        # Skip to the first seed of the current process' chunk,
        # multiply by 3 for x,y,z
        # Divide the generation to prevent RAM overuse
        seed_to_go = np.asscalar(first_seed_of_chunk) * 3
        while seed_to_go > 100000:
            random_generator.rand(100000)
            seed_to_go -= 100000
        random_generator.rand(seed_to_go)

        return random_generator, indices


class BinaryMask(object):
    """
    Mask class for binary mask.
    """

    def __init__(self, tracking_dataset):
        self.m = tracking_dataset
        # force memmap to array. needed for multiprocessing
        self.m.data = np.array(self.m.data)
        ndim = self.m.data.ndim
        if not (ndim == 3 or (ndim == 4 and self.m.data.shape[-1] == 1)):
            raise ValueError('mask cannot be more than 3d')

    def isPropagationContinues(self, pos):
        """
        The propagation continues if the position is within the mask.

        Parameters
        ----------
        pos : tuple
            3D positions.

        Return
        ------
        value: bool
            True if the position is inside the mask.
        """
        return (self.m.get_position_value(*pos) > 0
                and self.m.is_position_in_bound(*pos))

    def isStreamlineIncluded(self, pos):
        """
        If the propagation stoped, this function determines if the streamline
        is included in the tractogram. Always True for BinaryMask.

        Parameters
        ----------
        pos : tuple
            3D positions.

        Return
        ------
        value: bool
            Always True.
        """
        return True
