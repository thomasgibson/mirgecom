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
import numpy.linalg as la  # noqa
import pyopencl as cl

from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from meshmode.array_context import PyOpenCLArrayContext
from meshmode.dof_array import thaw

from mirgecom.initializers import Vortex2D
from mirgecom.initializers import Lump
from mirgecom.euler import split_conserved
from mirgecom.initializers import SodShock1D
from mirgecom.eos import IdealSingleGas

from grudge.eager import EagerDGDiscretization
from pyopencl.tools import (  # noqa
    pytest_generate_tests_for_pyopencl as pytest_generate_tests,
)
from pytools.obj_array import make_obj_array

def test_lump_init(ctx_factory):
    """
    Simple test to check that Lump initializer
    creates the expected solution field.
    """
    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    logger = logging.getLogger(__name__)

    dim = 2
    nel_1d = 4

    from meshmode.mesh.generation import generate_regular_rect_mesh

    mesh = generate_regular_rect_mesh(
        a=[(0.0,), (-5.0,)], b=[(10.0,), (5.0,)], n=(nel_1d,) * dim
    )

    order = 3
    logger.info(f"Number of elements: {mesh.nelements}")

    discr = EagerDGDiscretization(actx, mesh, order=order)
    nodes = thaw(actx, discr.nodes())

    # Init soln with Vortex
    center = np.zeros(shape=(dim,))
    velocity = np.zeros(shape=(dim,))
    center[0] = 5
    velocity[0] = 1
    lump = Lump(center=center, velocity=velocity)
    lump_soln = lump(0, nodes)

    eos = IdealSingleGas()
    p = eos.pressure(lump_soln)
    expected_p = 1.0

    pdiff = np.abs((p - make_obj_array([expected_p])))
    errmax = discr.norm(pdiff,p=np.inf)

    logger.info(f"lump_soln = {lump_soln}")
    logger.info(f"pressure = {p}")

    assert errmax < 1e-15


def test_vortex_init(ctx_factory):
    """
    Simple test to check that Vortex2D initializer
    creates the expected solution field.
    """
    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    logger = logging.getLogger(__name__)

    dim = 2
    nel_1d = 4

    from meshmode.mesh.generation import generate_regular_rect_mesh

    mesh = generate_regular_rect_mesh(
        a=[(0.0,), (-5.0,)], b=[(10.0,), (5.0,)], n=(nel_1d,) * dim
    )

    order = 3
    logger.info(f"Number of elements: {mesh.nelements}")

    discr = EagerDGDiscretization(actx, mesh, order=order)
    nodes = thaw(actx, discr.nodes())

    # Init soln with Vortex
    vortex = Vortex2D()
    vortex_soln = vortex(0, nodes)
    gamma = 1.4
    mass = split_conserved(dim, vortex_soln).mass
    eos = IdealSingleGas()
    p = eos.pressure(vortex_soln)

    exp_p = mass ** gamma
    pdiff = p - exp_p
    errmax = discr.norm(pdiff,p=np.inf)

    logger.info(f"vortex_soln = {vortex_soln}")
    logger.info(f"pressure = {p}")

    assert errmax < 1e-15


def test_shock_init(ctx_factory):
    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    nel_1d = 10
    dim = 2

    from meshmode.mesh.generation import generate_regular_rect_mesh

    mesh = generate_regular_rect_mesh(
        a=[(0.0,), (1.0,)], b=[(-0.5,), (0.5,)], n=(nel_1d,) * dim
    )

    order = 3
    print(f"Number of elements: {mesh.nelements}")

    discr = EagerDGDiscretization(actx, mesh, order=order)
    nodes = thaw(actx, discr.nodes())

    initr = SodShock1D()
    initsoln = initr(t=0.0, x_vec=nodes)
    print("Sod Soln:", initsoln)
    xpl = 1.0
    xpr = 0.1
    tol = 1e-15
    nodes_x = nodes[0]
    eos = IdealSingleGas()
    p = eos.pressure(initsoln)
    nel = len(p)
    # Check them all individually
    for i in range(nel):
        if nodes_x[i] < 0.5:
            assert np.abs(p[i] - xpl) < tol
        else:
            assert np.abs(p[i] - xpr) < tol
