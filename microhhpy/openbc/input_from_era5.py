#
#  MicroHH
#  Copyright (c) 2011-2024 Chiel van Heerwaarden
#  Copyright (c) 2011-2024 Thijs Heus
#  Copyright (c) 2014-2024 Bart van Stratum
#
#  This file is part of MicroHH
#
#  MicroHH is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  MicroHH is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with MicroHH.  If not, see <http://www.gnu.org/licenses/>.
#

# Standard library

# Third-party.
import numpy as np
from datetime import datetime

from concurrent.futures import ThreadPoolExecutor
import multiprocessing

# Local library
from microhhpy.logger import logger
from microhhpy.interpolate.interpolate_kernels import Rect_to_curv_interpolation_factors
from microhhpy.interpolate.interpolate_kernels import interpolate_rect_to_curv
from microhhpy.spatial import Vertical_grid_2nd

from .global_help_functions import gaussian_filter_wrapper, blend_w_to_zero_at_sfc
from .global_help_functions import correct_div_uv, calc_w_from_uv, check_divergence
from .lbc_help_functions import create_lbc_ds, setup_lbc_slices


def setup_interpolations(
        lon_era,
        lat_era,
        proj_pad,
        dtype):
    """
    Calculate horizontal interpolation factors at all staggered grid locations.
    Horizonal only, so `w` factors equal to scalar factors.

    Arguments:
    ---------
    lon_era : np.ndarray, shape (2,)
        Longitudes of ERA5 grid points.
    lat_era : np.ndarray, shape (2,)
        Latitudes of ERA5 grid points.
    proj_pad : microhhpy.spatial.Projection instance
        Spatial projection.
    dtype : numpy float type, optional
        Data type output arrays.

    Returns:
    -------
    dict
        Dictionary with `Rect_to_curv_interpolation_factors` instances.
    """

    if_u = Rect_to_curv_interpolation_factors(
        lon_era, lat_era, proj_pad.lon_u, proj_pad.lat_u, dtype)

    if_v = Rect_to_curv_interpolation_factors(
        lon_era, lat_era, proj_pad.lon_v, proj_pad.lat_v, dtype)

    if_s = Rect_to_curv_interpolation_factors(
        lon_era, lat_era, proj_pad.lon, proj_pad.lat, dtype)

    return dict(
        u = if_u,
        v = if_v,
        s = if_s
    )


def get_interpolation_factors(
        factors,
        name):
    """
    Get interpolation factors at correct staggered grid location.

    Arguments:
    ---------
    factors : dict
        Dictionary with `Rect_to_curv_interpolation_factors` instances.
    name : str
        Name of field.

    Returns:
    -------
        `Rect_to_curv_interpolation_factors` instance
    """

    if name == 'u':
        return factors['u']
    elif name == 'v':
        return factors['v']
    else:
        return factors['s']


def save_3d_field(
        fld,
        name,
        name_suffix,
        n_pad,
        output_dir):
    """
    Save 3D field without ghost cells to file.
    """
    if name_suffix == '':
        f_out = f'{output_dir}/{name}.0000000'
    else:
        f_out = f'{output_dir}/{name}_{name_suffix}.0000000'

    fld[:, n_pad:-n_pad, n_pad:-n_pad].tofile(f_out)


def parse_scalar(
    lbc_ds,
    name,
    name_suffix,
    t,
    fld_era,
    z_era,
    z_les,
    ip_fac,
    lbc_slices,
    sigma_n,
    domain,
    output_dir,
    dtype):
    """
    Parse a single scalar for a single time step.
    Creates both the initial field (t=0 only) and lateral boundary conditions.

    Parameters
    ----------
    lbc_ds : xarray.Dataset
        Dataset containing lateral boundary condition (LBC) fields.
    name : str
        Name of the scalar field.
    name_suffix : str
        Suffix to append to the output variable name.
    t : int
        Timestep index.
    fld_era : np.ndarray, shape (3,)
        Scalar field from ERA5.
    z_era : np.ndarray, shape (3,)
        Model level heights ERA5.
    z_les : np.ndarray, shape (1,)
        Full level heights LES.
    ip_fac : `Rect_to_curv_interpolation_factors` instance.
        Interpolation factors.
    lbc_slices : dict
        Dictionary with Numpy slices for each LBC.
    sigma_n : int
        Width Gaussian filter kernel in LES grid points.
    domain : Domain instance
        Domain information.
    output_dir : str
        Output directory.
    dtype : np.float32 or np.float64
        Floating point precision.

    Returns
    -------
    None
    """
    logger.debug(f'Processing field {name} at t={t}.')

    # Keep creation of 3D field here, for parallel/async exectution..
    fld_les = np.empty((z_les.size, domain.proj_pad.jtot, domain.proj_pad.itot), dtype=dtype)

    # Tri-linear interpolation from ERA5 to LES grid.
    interpolate_rect_to_curv(
        fld_les,
        fld_era,
        ip_fac.il,
        ip_fac.jl,
        ip_fac.fx,
        ip_fac.fy,
        z_les,
        z_era,
        dtype)
    
    # Apply Gaussian filter.
    if sigma_n > 0:
        gaussian_filter_wrapper(fld_les, sigma_n)
    
    # Save 3D field without ghost cells in binary format as initial/restart file.
    if t == 0:
        save_3d_field(fld_les, name, name_suffix, domain.n_pad, output_dir)

    # Save lateral boundaries.
    for loc in ('west', 'east', 'south', 'north'):
        lbc_slice = lbc_slices[f's_{loc}']
        lbc_ds[f'{name}_{loc}'][t,:,:,:] = fld_les[lbc_slice]


def parse_momentum(
    lbc_ds,
    name_suffix,
    t,
    u_era,
    v_era,
    w_era,
    z_era,
    z,
    zh,
    dz,
    dzi,
    rho,
    rhoh,
    ip_u,
    ip_v,
    ip_s,
    lbc_slices,
    sigma_n,
    domain,
    output_dir,
    dtype):
    """
    Parse all momentum fields for a single time step..
    Creates both the initial field (t=0 only) and lateral boundary conditions.

    Steps:
    1. Blend w to zero to surface over a certain (500 m) depth.
    2. Correct horizontal divergence of u and v to match subsidence in LES to ERA5.
    3. Calculate new vertical velocity w to ensure that the fields are divergence free.
    4. Check resulting divergence.

    Parameters
    ----------
    lbc_ds : xarray.Dataset
        Dataset containing lateral boundary condition (LBC) fields.
    name_suffix : str
        Suffix to append to the output variable name.
    t : int
        Timestep index.
    u_era : np.ndarray, shape (3,)
        u-field from ERA5.
    v_era : np.ndarray, shape (3,)
        v-field from ERA5.
    w_era : np.ndarray, shape (3,)
        w-field from ERA5.
    z_era : np.ndarray, shape (3,)
        Model level heights ERA5.
    z : np.ndarray, shape (1,)
        Full level heights LES.
    zh : np.ndarray, shape (1,)
        Half level heights LES.
    dz : np.ndarray, shape (1,)
        Full level grid spacing LES.
    dzi : np.ndarray, shape (1,)
        Inverse of full level grid spacing LES.
    rho : np.ndarray, shape (1,)
        Full level base state density.
    rhoh : np.ndarray, shape (1,)
        Half level base state density.
    ip_u : `Rect_to_curv_interpolation_factors` instance.
        Interpolation factors at u location.
    ip_v : `Rect_to_curv_interpolation_factors` instance.
        Interpolation factors at v location.
    ip_s : `Rect_to_curv_interpolation_factors` instance.
        Interpolation factors at scalar location.
    lbc_slices : dict
        Dictionary with Numpy slices for each LBC.
    sigma_n : int
        Width Gaussian filter kernel in LES grid points.
    domain : Domain instance
        Domain information.
    output_dir : str
        Output directory.
    dtype : np.float32 or np.float64
        Floating point precision.

    Returns
    -------
    None
    """
    logger.debug(f'Processing momentum at t={t}.')

    # Keep creation of 3D field here, for parallel/async exectution..
    u = np.empty((z.size,  domain.proj_pad.jtot, domain.proj_pad.itot), dtype=dtype)
    v = np.empty((z.size,  domain.proj_pad.jtot, domain.proj_pad.itot), dtype=dtype)
    w = np.empty((zh.size, domain.proj_pad.jtot, domain.proj_pad.itot), dtype=dtype)

    # Tri-linear interpolation from ERA5 to LES grid.
    interpolate_rect_to_curv(
        u,
        u_era,
        ip_u.il,
        ip_u.jl,
        ip_u.fx,
        ip_u.fy,
        z,
        z_era,
        dtype)

    interpolate_rect_to_curv(
        v,
        v_era,
        ip_v.il,
        ip_v.jl,
        ip_v.fx,
        ip_v.fy,
        z,
        z_era,
        dtype)

    interpolate_rect_to_curv(
        w,
        w_era,
        ip_s.il,
        ip_s.jl,
        ip_s.fx,
        ip_s.fy,
        zh,
        z_era,
        dtype)
    
    # Apply Gaussian filter.
    if sigma_n > 0:
        gaussian_filter_wrapper(u, sigma_n)
        gaussian_filter_wrapper(v, sigma_n)
        gaussian_filter_wrapper(w, sigma_n)


    # ERA5 vertical velocity `w_era` sometimes has strange profiles near surface.
    # Blend linearly to zero. This also insures that w at the surface is 0.0 m/s.
    blend_w_to_zero_at_sfc(w, zh, zmax=500)

    # Correct horizontal divergence of u and v.
    proj = domain.proj_pad
    correct_div_uv(
        u,
        v,
        w,
        rho,
        rhoh,
        dzi,
        proj.x,
        proj.y,
        proj.xsize,
        proj.ysize,
        domain.n_pad)

    # NOTE: correcting the horizontal divergence only ensures that the _mean_ vertical velocity
    # is correct. We still need to calculate a new vertical velocity to ensure that the wind
    # fields are divergence free at a grid point level.
    calc_w_from_uv(
        w,
        u,
        v,
        rho,
        rhoh,
        dz,
        domain.dxi,
        domain.dyi,
        domain.istart_pad,
        domain.iend_pad,
        domain.jstart_pad,
        domain.jend_pad,
        dz.size)

    # Check! 
    div_max, i, j, k = check_divergence(
        u,
        v,
        w,
        rho,
        rhoh, 
        domain.dxi,
        domain.dyi,
        dzi,
        domain.istart_pad,
        domain.iend_pad,
        domain.jstart_pad,
        domain.jend_pad,
        dz.size)
    logger.debug(f'Maximum divergence in LES domain: {div_max:.3e} at i={i}, j={j}, k={k}')

    # Save 3D field without ghost cells in binary format as initial/restart file.
    if t == 0:
        save_3d_field(u[:  ,:,:], 'u', name_suffix, domain.n_pad, output_dir)
        save_3d_field(v[:  ,:,:], 'v', name_suffix, domain.n_pad, output_dir)
        save_3d_field(w[:-1,:,:], 'w', name_suffix, domain.n_pad, output_dir)

    # Save lateral boundaries.
    for loc in ('west', 'east', 'south', 'north'):
        lbc_slice = lbc_slices[f'u_{loc}']
        lbc_ds[f'u_{loc}'][t,:,:,:] = u[lbc_slice]

        lbc_slice = lbc_slices[f'v_{loc}']
        lbc_ds[f'v_{loc}'][t,:,:,:] = v[lbc_slice]

        lbc_slice = lbc_slices[f'w_{loc}']
        lbc_ds[f'w_{loc}'][t,:,:,:] = w[lbc_slice]


def create_era5_input(
        fields_era,
        lon_era,
        lat_era,
        z_era,
        time_era,
        z,
        zsize,
        rho,
        rhoh,
        domain,
        sigma_h,
        name_suffix='',
        output_dir='.',
        ntasks=8,
        dtype=np.float64):
    """
    Generate all required MicroHH input from ERA5.
    
    1. Initial fields.
    2. Lateral boundary conditions.
    3. ...
    """
    logger.info(f'Creating MicroHH input from ERA5 data in {output_dir}.')

    # Short-cuts.
    proj_pad = domain.proj_pad

    # Setup vertical grid. Definition has to perfectly match MicroHH's vertical grid to get divergence free fields.
    vgrid = Vertical_grid_2nd(z, zsize, remove_ghost=True, dtype=dtype)

    # Setup horizontal interpolations (indexes and factors).
    ip_facs = setup_interpolations(lon_era, lat_era, proj_pad, dtype=dtype)

    # Setup spatial filtering.
    sigma_n = int(np.ceil(sigma_h / proj_pad.dx))
    if sigma_n > 0:
        logger.info(f'Using Gaussian filter with sigma = {sigma_n} grid cells')

    # Setup lateral boundary fields.
    lbc_ds = create_lbc_ds(
        list(fields_era.keys()),
        time_era,
        domain.x,
        domain.y,
        vgrid.z,
        domain.xh,
        domain.yh,
        vgrid.zh[:-1],
        domain.n_ghost,
        domain.n_sponge,
        dtype=dtype)

    # Numpy slices of lateral boundary conditions.
    lbc_slices = setup_lbc_slices(domain.n_ghost, domain.n_sponge)


    """
    Parse scalars.
    This creates the initial fields for t=0 and lateral boundary conditions for all times.
    """
    ip_fac = get_interpolation_factors(ip_facs, 's')

    # Run in parallel with ThreadPoolExecutor for ~10x speed-up.
    args = []
    for name, fld_era in fields_era.items():
        if name not in ('u', 'v', 'w'):
            for t in range(time_era.size):
                args.append((
                    lbc_ds,
                    name,
                    name_suffix,
                    t,
                    fld_era[t,:,:,:],
                    z_era[t,:,:,:],
                    vgrid.z,
                    ip_fac,
                    lbc_slices,
                    sigma_n,
                    domain,
                    output_dir,
                    dtype))

    def parse_scalar_wrapper(args):
        return parse_scalar(*args)

    tick = datetime.now()

    with ThreadPoolExecutor(max_workers=ntasks) as executor:
        results = list(executor.map(parse_scalar_wrapper, args))

    tock = datetime.now()
    logger.info(f'Created scalar input from ERA5 in {tock - tick}.')


    """
    Parse momentum fields.
    This is treated separately, because it requires some corrections to ensure that the fields are divergence free.
    """
    if any(fld not in fields_era for fld in ('u', 'v', 'w')):
        logger.warning('One or more momentum fields missing! Skipping momentum...')
    else:
        ip_u = get_interpolation_factors(ip_facs, 'u')
        ip_v = get_interpolation_factors(ip_facs, 'v')
        ip_s = get_interpolation_factors(ip_facs, 's')

        # Run in parallel with ThreadPoolExecutor for ~10x speed-up.
        args = []
        for t in range(time_era.size):
            args.append((
                lbc_ds,
                name_suffix,
                t,
                fields_era['u'][t,:,:,:],
                fields_era['v'][t,:,:,:],
                fields_era['w'][t,:,:,:],
                z_era[t,:,:,:],
                vgrid.z,
                vgrid.zh,
                vgrid.dz,
                vgrid.dzi,
                rho,
                rhoh,
                ip_u,
                ip_v,
                ip_s,
                lbc_slices,
                sigma_n,
                domain,
                output_dir,
                dtype))

        def parse_momentum_wrapper(args):
            return parse_momentum(*args)

        tick = datetime.now()

        with ThreadPoolExecutor(max_workers=ntasks) as executor:
            results = list(executor.map(parse_momentum_wrapper, args))

        tock = datetime.now()
        logger.info(f'Created momentum input from ERA5 in {tock - tick}.')




#def process_momentum(
#        u,
#        v,
#        w,
#        rho,
#        rhoh,
#        zh,
#        dz,
#        dzi,
#        domain,
#        n_pad
#        ):
#    """
#    Process interpolated momentum fields:
#    1. Blend w to zero to surface over a certain (500 m) depth.
#    2. Correct horizontal divergence of u and v to match subsidence in LES to ERA5.
#    3. Calculate new vertical velocity w to ensure that the fields are divergence free.
#    4. Check resulting divergence.
#
#    Arguments:
#    ---------
#    u : np.ndarray, shape (3,)
#        Zonal wind field.
#    v : np.ndarray, shape (3,)
#        Meridional wind field
#    w : np.ndarray, shape (1,)
#        Vertical wind field.
#    rho : np.ndarray, shape (1,)
#        Basestate air density at LES full levels.
#    rhoh : np.ndarray, shape (1,)
#        Basestate air density at LES half levels.
#    zh : np.ndarray, shape (1,)
#        LES half-level heights.
#    dz : np.ndarray, shape (1,)
#        Vertical grid spacing full levels.
#    dzi : np.ndarray, shape (1,)
#        Inverse of `dz`.
#    domain : Domain instance
#        microhhpy.domain.Domain instance, needed for spatial transforms.
#    n_pad : int
#        Number of ghost cells including padding.
#
#    Returns:
#        None
#    """
#    proj_pad = domain.proj_pad
#    
#    # Interpolated ERA5 `w_ls` sometimes has strange profiles near surface.
#    # Blend linearly to zero. This also insures that w at the surface is 0.0 m/s.
#    logger.debug(f'Blending w to zero at the surface.')
#    blend_w_to_zero_at_sfc(w, zh, zmax=500)
#
#    # Correct horizontal divergence of u and v.
#    correct_div_uv(
#        u,
#        v,
#        w,
#        rho,
#        rhoh,
#        dzi,
#        proj_pad.x,
#        proj_pad.y,
#        proj_pad.xsize,
#        proj_pad.ysize,
#        n_pad)
#
#    # NOTE: correcting the horizontal divergence only ensures that the _mean_ vertical velocity
#    # is correct. We still need to calculate a new vertical velocity to ensure that the wind
#    # fields are divergence free at a grid point level.
#    logger.debug(f'Calculating new vertical velocity to create divergence free wind fields.')
#    calc_w_from_uv(
#        w,
#        u,
#        v,
#        rho,
#        rhoh,
#        dz,
#        domain.dxi,
#        domain.dyi,
#        domain.istart_pad,
#        domain.iend_pad,
#        domain.jstart_pad,
#        domain.jend_pad,
#        dz.size)
#
#    # Check! 
#    div_max, i, j, k = check_divergence(
#        u,
#        v,
#        w,
#        rho,
#        rhoh, 
#        domain.dxi,
#        domain.dyi,
#        dzi,
#        domain.istart_pad,
#        domain.iend_pad,
#        domain.jstart_pad,
#        domain.jend_pad,
#        dz.size)
#    logger.debug(f'Maximum divergence in LES domain: {div_max:.3e} at i={i}, j={j}, k={k}')
#
#
#def create_fields_from_era5(
#        fields_era,
#        lon_era,
#        lat_era,
#        z_era,
#        z,
#        zh,
#        dz,
#        dzi,
#        rho,
#        rhoh,
#        domain,
#        correct_div_h,
#        sigma_h,
#        name_suffix='',
#        output_dir='.',
#        dtype=np.float64):
#    """
#    Generate initial LES fields by interpolating ERA5 data onto the LES grid.
#
#    If requested, it also corrects the horizontal divergence of the wind fields,
#    to ensure that the domain mean vertical velocity in LES matches the
#    subsidence velocity of ERA5, while still being divergence free at the grid point level.
#
#    The generated 3D fields are saved in binary format without ghost cells to `output_dir`.
#
#    Arguments:
#    ---------
#    fields_era : dict of np.ndarray
#        Dictionary with ERA5 fields.
#    lon_era : np.ndarray, shape (2,)
#        Longitudes of ERA5 grid points.
#    lat_era : np.ndarray, shape (2,)
#        Latitudes of ERA5 grid points.
#    z_era : np.ndarray, shape (3,)
#        Heights of ERA5 full levels.
#    z : np.ndarray, shape (1,)
#        LES full-level heights.
#    zh : np.ndarray, shape (1,)
#        LES half-level heights.
#    dz : np.ndarray, shape (1,)
#        LES vertical grid spacing.
#    dzi : np.ndarray, shape (1,)
#        Inverse of `dz`.
#    rho : np.ndarray, shape (1,)
#        Basestate air density at LES full levels.
#    rhoh : np.ndarray, shape (1,)
#        Basestate air density at LES half levels.
#    domain : Domain instance
#        microhhpy.domain.Domain instance, needed for spatial transforms.
#    correct_div_h : bool
#        If True, apply horizontal divergence correction to match target subsidence.
#    sigma_h : float
#        Width of Gaussian filter for smoothing the interpolated fields (in horizontal distance units).
#    name_suffix : str, optional
#        String to append to output filenames.
#    output_dir : str, optional
#        Directory to write the output files.
#    dtype : numpy float type, optional
#        Data type for output arrays. Defaults to `np.float64`.
#
#    Returns:
#    -------
#    None
#    """
#
#    """
#    Inline/lambda help functions.
#    """
#    def save_field(fld, name):
#        """
#        Save 3D field without ghost cells to file.
#        """
#        if name_suffix == '':
#            f_out = f'{output_dir}/{name}.0000000'
#        else:
#            f_out = f'{output_dir}/{name}_{name_suffix}.0000000'
#
#        fld[s_int].tofile(f_out)
#
#
#    def interpolate(fld_les, name):
#        """
#        Tri-linear interpolation of ERA5 to LES grid,
#        """
#        logger.debug(f'Interpolating initial field {name} from ERA to LES')
#
#        if_loc = get_interpolation_factors(ip_facs, name)
#        z_loc = zh if name == 'w' else z
#
#        # Tri-linear interpolation from ERA5 to LES grid.
#        interpolate_rect_to_curv(
#            fld_les,
#            fields_era[name],
#            if_loc.il,
#            if_loc.jl,
#            if_loc.fx,
#            if_loc.fy,
#            z_loc,
#            z_era,
#            dtype)
#
#        # Apply Gaussian filter.
#        if sigma_n > 0:
#            gaussian_filter_wrapper(fld_les, sigma_n)
#
#        
#    proj_pad = domain.proj_pad
#    n_pad = domain.n_pad
#
#    """
#    Setup interpolation factors.
#    """
#    ip_facs = setup_interpolations(lon_era, lat_era, proj_pad, dtype)
#
#    """
#    Define fields with ghost cells, needed to make u,v,w, divergence free.
#    """
#    dims_full = (z.size,  proj_pad.jtot, proj_pad.itot)
#    dims_half = (zh.size, proj_pad.jtot, proj_pad.itot)
#
#    fld_full = np.empty(dims_full, dtype=dtype)
#    fld_half = np.empty(dims_half, dtype=dtype)
#
#    # Numpy slice with interior of domain.
#    s_int = np.s_[:, n_pad:-n_pad, n_pad:-n_pad]
#
#    
#    """
#    Setup spatial filtering.
#    """
#    sigma_n = int(np.ceil(sigma_h / (6 * proj_pad.dx)))
#    if sigma_n > 0:
#        logger.debug(f'Using Gaussian filter with sigma = {sigma_n} grid cells')
#
#
#    """
#    Parse the momentum fields separately if divergence correction is requested.
#    """
#    if correct_div_h:
#
#        if not all(fld in fields_era for fld in ['u', 'v', 'w']):
#            logger.critical('Requested divergence correction, but u, v, or w missing!')
#
#        u = np.empty(dims_full, dtype=dtype)
#        v = np.empty(dims_full, dtype=dtype)
#        w = np.empty(dims_half, dtype=dtype)
#
#        interpolate(u, 'u')
#        interpolate(v, 'v')
#        interpolate(w, 'w')
#
#        process_momentum(
#            u,
#            v,
#            w,
#            rho,
#            rhoh,
#            zh,
#            dz,
#            dzi,
#            domain,
#            n_pad)
#
#        save_field(u, 'u')
#        save_field(v, 'v')
#        save_field(w, 'w')
#
#
#    """
#    Parse remaining fields.
#    """
#    exclude_fields = ('u', 'v', 'w') if correct_div_h else ()
#
#    for name, fld_era in fields_era.items():
#        if name not in exclude_fields:
#
#            fld = fld_half if name == 'w' else fld_full
#            
#            # Tri-linear interpolation from ERA5 to LES grid.
#            interpolate(fld, name)
#
#            # Save 3D field without ghost cells in binary format.
#            save_field(fld, name)
#
#
#def create_lbcs_from_era5(
#        fields_era,
#        lon_era,
#        lat_era,
#        z_era,
#        time,
#        start_date,
#        z,
#        zh,
#        dz,
#        dzi,
#        rho,
#        rhoh,
#        domain,
#        n_sponge,
#        correct_div_h,
#        sigma_h,
#        output_dir='.',
#        dtype=np.float64):
#    """
#    Docstring TODO
#    """
#
#    """
#    Inline/lambda help functions.
#    """
#    def get_interpolation_factors(name):
#        """
#        Get interpolation factors for a given field name.
#        """
#        if name == 'u':
#            return if_u
#        elif name == 'v':
#            return if_v
#        else:
#            return if_s
#
#
#    def interpolate(fld_les, name):
#        """
#        Tri-linear interpolation of ERA5 to LES grid,
#        """
#        logger.debug(f'Interpolating field {name} from ERA to LES')
#
#        if_loc = get_interpolation_factors(name)
#        z_loc = zh if name == 'w' else z
#
#        # Tri-linear interpolation from ERA5 to LES grid.
#        interpolate_rect_to_curv(
#            fld_les,
#            fields_era[name],
#            if_loc.il,
#            if_loc.jl,
#            if_loc.fx,
#            if_loc.fy,
#            z_loc,
#            z_era,
#            dtype)
#
#        # Apply Gaussian filter.
#        if sigma_n > 0:
#            gaussian_filter_wrapper(fld_les, sigma_n)
#
#
#    n_pad = domain.n_pad
#    proj_pad = domain.proj_pad
#
#
#    """
#    Setup Xarray dataset with correction dimensions/coordinates of lateratal boundary conditions.
#    """
#    lbc_ds = create_lbc_ds(
#        list(fields_era.keys()),
#        domain.x,
#        domain.y,
#        z,
#        domain.xh,
#        domain.yh,
#        zh,
#        time,
#        start_date,
#        domain.n_ghost,
#        n_sponge,
#        dtype=dtype)
#
#
#    """
#    Numpy slices of all boundary fields.
#    """
#    slices = dict(
#            ss_west = np.s_[:, 1:-1, 1:n_pad],
#            ss_east = np.s_[:, 1:-1, -n_pad:-1],
#            ss_south = np.s_[:, 1:n_pad, 1:-1],
#            ss_north = np.s_[:, -n_pad:-1, 1:-1],
#
#            su_west = np.s_[:, 1:-1, 1:n_pad+1],
#            su_east = np.s_[:, 1:-1, -n_pad:-1],
#            su_south = np.s_[:, 1:n_pad, 1:-1],
#            su_north = np.s_[:, -n_pad:-1, 1:-1],
#
#            sv_west = np.s_[:, 1:-1, 1:n_pad],
#            sv_east = np.s_[:, 1:-1, -n_pad:-1],
#            sv_south = np.s_[:, 1:n_pad+1, 1:-1],
#            sv_north = np.s_[:, -n_pad:-1, 1:-1],
#
#            sw_west = np.s_[:-1, 1:-1, 1:n_pad],
#            sw_east = np.s_[:-1, 1:-1, -n_pad:-1],
#            sw_south = np.s_[:-1, 1:n_pad, 1:-1],
#            sw_north = np.s_[:-1, -n_pad:-1, 1:-1])
#
#
#    """
#    Calculate horizontal interpolation factors at all staggered grid locations.
#    Horizonal only, so `w` factors equal to scalar factors.
#    """
#    if_u = Rect_to_curv_interpolation_factors(
#        lon_era, lat_era, proj_pad.lon_u, proj_pad.lat_u, dtype)
#
#    if_v = Rect_to_curv_interpolation_factors(
#        lon_era, lat_era, proj_pad.lon_v, proj_pad.lat_v, dtype)
#
#    if_s = Rect_to_curv_interpolation_factors(
#        lon_era, lat_era, proj_pad.lon, proj_pad.lat, dtype)
#
#    
#    """
#    Fields are interpolated over full 3D fields, not just the boundaries.
#    This is a bit wasteful, but a lot easier with the spatial filtering and all.
#    """
#    dims_full = (z.size,  proj_pad.jtot, proj_pad.itot)
#    dims_half = (zh.size, proj_pad.jtot, proj_pad.itot)
#
#    fld_full = np.empty(dims_full, dtype=dtype)
#    fld_half = np.empty(dims_half, dtype=dtype)
#
#    # Filter size from meters to `n` grid cells.
#    sigma_n = int(np.ceil(sigma_h / (6 * proj_pad.dx)))
#    if sigma_n > 0:
#        logger.debug(f'Using Gaussian filter with sigma = {sigma_n} grid cells')
#
#
#    """
#    Process all time steps.
#    """
#    for t in range(time.size):
#        logger.debug(f'Processing time step {t+1}/{time.size}')