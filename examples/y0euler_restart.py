"""mirgecom driver for the Y0 demonstration.

Note: this example requires a *scaled* version of the Y0
grid. A working grid example is located here:
github.com:/illinois-ceesd/data@y0scaled
"""

__copyright__ = """
Copyright (C) 2020 University of Illinois Board of Trustees
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import logging
import numpy as np
import pyopencl as cl
import numpy.linalg as la  # noqa
import pyopencl.array as cla  # noqa
from functools import partial
from mpi4py import MPI
import math

from pytools.obj_array import obj_array_vectorize
import pickle

from meshmode.array_context import PyOpenCLArrayContext
from meshmode.dof_array import thaw, flatten, unflatten
from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from grudge.eager import EagerDGDiscretization
from grudge.shortcuts import make_visualizer


from mirgecom.euler import inviscid_operator
from mirgecom.simutil import (
    inviscid_sim_timestep,
    sim_checkpoint,
    check_step,
    create_parallel_grid
)
from mirgecom.io import (
    make_init_message,
)
# from mirgecom.checkstate import compare_states
from mirgecom.integrators import rk4_step
from mirgecom.steppers import advance_state
from mirgecom.boundary import (
    PrescribedBoundary,
    AdiabaticSlipBoundary,
    DummyBoundary
)
from mirgecom.initializers import Lump
from mirgecom.eos import IdealSingleGas


def get_pseudo_y0_mesh():
    """Generate or import a grid using `gmsh`.

    Input required:
        data/pseudoY0.brep  (for mesh gen)
        -or-
        data/pseudoY0.msh   (read existing mesh)

    This routine will generate a new grid if it does
    not find the grid file (data/pseudoY0.msh), but
    note that if the grid is generated in millimeters,
    then the solution initialization and BCs need to be
    adjusted or the grid needs to be scaled up to meters
    before being used with the current main driver in this
    example.
    """
    from meshmode.mesh.io import (
        read_gmsh,
        generate_gmsh,
        ScriptWithFilesSource
    )
    import os
    if os.path.exists("data/pseudoY0.msh") is False:
        mesh = generate_gmsh(
            ScriptWithFilesSource("""
            Merge "data/pseudoY0.brep";
            Mesh.CharacteristicLengthMin = 1;
            Mesh.CharacteristicLengthMax = 10;
            Mesh.ElementOrder = 2;
            Mesh.CharacteristicLengthExtendFromBoundary = 0;

            // Inside and end surfaces of nozzle/scramjet
            Field[1] = Distance;
            Field[1].NNodesByEdge = 100;
            Field[1].FacesList = {5,7,8,9,10};
            Field[2] = Threshold;
            Field[2].IField = 1;
            Field[2].LcMin = 1;
            Field[2].LcMax = 10;
            Field[2].DistMin = 0;
            Field[2].DistMax = 20;

            // Edges separating surfaces with boundary layer
            // refinement from those without
            // (Seems to give a smoother transition)
            Field[3] = Distance;
            Field[3].NNodesByEdge = 100;
            Field[3].EdgesList = {5,10,14,16};
            Field[4] = Threshold;
            Field[4].IField = 3;
            Field[4].LcMin = 1;
            Field[4].LcMax = 10;
            Field[4].DistMin = 0;
            Field[4].DistMax = 20;

            // Min of the two sections above
            Field[5] = Min;
            Field[5].FieldsList = {2,4};

            Background Field = 5;
        """, ["data/pseudoY0.brep"]), 3, target_unit="MM")
    else:
        mesh = read_gmsh("data/pseudoY0.msh")

    return mesh


def main(ctx_factory=cl.create_some_context,
         snapshot_pattern="wave-eager-{step:04d}-{rank:04d}.pkl",
         restart_step=None):
    """Drive the Y0 example."""
    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    logger = logging.getLogger(__name__)

    dim = 3
    order = 1
    t_final = 1.e-8
    current_cfl = 1.0
    vel_init = np.zeros(shape=(dim,))
    vel_inflow = np.zeros(shape=(dim,))
    orig = np.zeros(shape=(dim,))
    orig[0] = 0.83
    orig[2] = 0.001
    #    vel[0] = 340.0
    vel_inflow[0] = 100.0  # m/s
    current_dt = 1e-11
    casename = "pseudoY0"
    constant_cfl = False
    nstatus = 1
    nviz = 1
    nrestart = 100  # steps
    rank = 0

    # working gas: CO2 #
    #   gamma = 1.289
    #   MW=44.009  g/mol
    #   cp = 37.135 J/mol-K,
    #   rho= 1.977 kg/m^3 @298K
    gamma_co2 = 1.289
    r_co2 = 8314.59/44.009

    # background
    #   100 Pa
    #   298 K
    #   rho = 1.77619667e-3 kg/m^3
    #   velocity = 0,0,0
    rho_bkrnd = 1.77619667e-3
    pres_bkrnd = 100
    #    temp_bkrnd = 298

    # nozzle inflow #
    #
    # stagnation tempertuare 298 K
    # stagnation pressure 1.5e Pa
    #
    # isentropic expansion based on the area ratios between the inlet (r=13e-3m) and
    # the throat (r=6.3e-3)
    #
    #  MJA, this is calculated offline, add some code to do it for us
    #
    #   Mach number=0.139145
    #   pressure=148142
    #   temperature=297.169
    #   density=2.63872
    #   gamma=1.289
    pres_inflow = 148142
    #    temp_inflow = 297.169
    rho_inflow = 2.63872
    mach_inflow = 0.139145
    vel_inflow[0] = mach_inflow*math.sqrt(gamma_co2*pres_inflow/rho_inflow)

    timestepper = rk4_step
    eos = IdealSingleGas(gamma=gamma_co2, gas_const=r_co2)
    wall = AdiabaticSlipBoundary()
    dummy = DummyBoundary()
    bulk_init = Lump(numdim=dim, rho0=rho_bkrnd, p0=pres_bkrnd,
                     center=orig, velocity=vel_init, rhoamp=0.0)
    inflow_init = Lump(numdim=dim, rho0=rho_inflow, p0=pres_inflow,
                       center=orig, velocity=vel_inflow, rhoamp=0.0)

    from grudge import sym
#    boundaries = {BTAG_ALL: DummyBoundary}
    boundaries = {sym.DTAG_BOUNDARY("Inflow"): PrescribedBoundary(inflow_init),
                  sym.DTAG_BOUNDARY("Outflow"): dummy,
                  sym.DTAG_BOUNDARY("Wall"): wall}

    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    nparts = comm.Get_size()

    current_t = 0
    checkpoint_t = current_t
    current_step = 0

    if restart_step is None:
        local_mesh, global_nelements = create_parallel_grid(comm,
                                                            get_pseudo_y0_mesh)
        local_nelements = local_mesh.nelements

    else:  # Restart
        with open(snapshot_pattern.format(step=restart_step, rank=rank), "rb") as f:
            restart_data = pickle.load(f)

        local_mesh = restart_data["local_mesh"]
        local_nelements = local_mesh.nelements
        global_nelements = restart_data["global_nelements"]
        assert comm.Get_size() == restart_data["nparts"]

    if rank == 0:
        logging.info("Making discretization")
    discr = EagerDGDiscretization(
        actx, local_mesh, order=order, mpi_communicator=comm
    )
    nodes = thaw(actx, discr.nodes())

    if restart_step is None:
        if rank == 0:
            logging.info("Initializing soln.")
        current_state = bulk_init(0, nodes, eos=eos)
    else:
        current_t = restart_data["t"]
        current_step = restart_step

        current_state = unflatten(
            actx, discr.discr_from_dd("vol"),
            obj_array_vectorize(actx.from_numpy, restart_data["state"]))

    visualizer = make_visualizer(discr, discr.order + 3
                                 if discr.dim == 2 else discr.order)

    initname = "pseudoY0"
    eosname = eos.__class__.__name__
    init_message = make_init_message(dim=dim, order=order,
                                     nelements=local_nelements,
                                     global_nelements=global_nelements,
                                     dt=current_dt, t_final=t_final,
                                     nstatus=nstatus, nviz=nviz,
                                     cfl=current_cfl,
                                     constant_cfl=constant_cfl,
                                     initname=initname,
                                     eosname=eosname, casename=casename)
    if rank == 0:
        logger.info(init_message)

    get_timestep = partial(inviscid_sim_timestep, discr=discr, t=current_t,
                           dt=current_dt, cfl=current_cfl, eos=eos,
                           t_final=t_final, constant_cfl=constant_cfl)

    def my_rhs(t, state):
        return inviscid_operator(discr, eos=eos, boundaries=boundaries,
                                 q=state, t=t)

    def my_checkpoint(step, t, dt, state):
        write_restart = (check_step(step, nrestart)
                         if step != restart_step else False)
        if write_restart is True:
            with open(snapshot_pattern.format(step=step, rank=rank), "wb") as f:
                pickle.dump({
                    "local_mesh": local_mesh,
                    "state": obj_array_vectorize(actx.to_numpy, flatten(state)),
                    "t": t,
                    "step": step,
                    "global_nelements": global_nelements,
                    "num_parts": nparts,
                    }, f)
        return sim_checkpoint(discr=discr, visualizer=visualizer, eos=eos,
                              q=state, vizname=casename,
                              step=step, t=t, dt=dt, nstatus=nstatus,
                              nviz=nviz, constant_cfl=constant_cfl, comm=comm)

    if rank == 0:
        logging.info("Stepping.")

    (current_step, current_t, current_state) = \
        advance_state(rhs=my_rhs, timestepper=timestepper,
                      checkpoint=my_checkpoint,
                      get_timestep=get_timestep, state=current_state,
                      t_final=t_final, t=current_t, istep=current_step)

    if rank == 0:
        logger.info("Checkpointing final state ...")

    my_checkpoint(current_step, t=current_t,
                  dt=(current_t - checkpoint_t),
                  state=current_state)

    if current_t - t_final < 0:
        raise ValueError("Simulation exited abnormally")


if __name__ == "__main__":
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    main()

# vim: foldmethod=marker