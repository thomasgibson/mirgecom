"""Demonstrate wave-lazy serial example."""

__copyright__ = "Copyright (C) 2020 University of Illinos Board of Trustees"

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

import numpy as np
import numpy.linalg as la  # noqa
import pyopencl as cl
import pyopencl.array as cla  # noqa
from pytools.obj_array import flat_obj_array
#         obj_array_vectorize)
from grudge.eager import EagerDGDiscretization
# from grudge.shortcuts import make_visualizer
from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from mirgecom.wave import wave_operator
from mirgecom.integrators import rk4_step
from meshmode.dof_array import thaw, freeze
from meshmode.array_context import PytatoArrayContext
import pyopencl.tools as cl_tools

from mirgecom.profiling import PyOpenCLProfilingArrayContext


def bump(actx, discr, t=0):
    """Create a bump."""
    source_center = np.array([0.2, 0.35, 0.1])[:discr.dim]
    source_width = 0.05
    source_omega = 3

    nodes = thaw(actx, discr.nodes())
    center_dist = flat_obj_array([
        nodes[i] - source_center[i]
        for i in range(discr.dim)
        ])

    return (
        np.cos(source_omega*t)
        * actx.np.exp(
            -np.dot(center_dist, center_dist)
            / source_width**2))


def main(use_profiling=False):
    """Drive the example."""
    cl_ctx = cl.create_some_context()
    if use_profiling:
        queue = cl.CommandQueue(cl_ctx,
            properties=cl.command_queue_properties.PROFILING_ENABLE)
        actx = PyOpenCLProfilingArrayContext(queue,
            allocator=cl_tools.MemoryPool(cl_tools.ImmediateAllocator(queue)))
    else:
        queue = cl.CommandQueue(cl_ctx)
        actx = PytatoArrayContext(queue)

    dim = 2
    nel_1d = 16
    from meshmode.mesh.generation import generate_regular_rect_mesh

    mesh = generate_regular_rect_mesh(
        a=(-0.5,)*dim,
        b=(0.5,)*dim,
        n=(nel_1d,)*dim)

    order = 3

    if dim == 2:
        # no deep meaning here, just a fudge factor
        dt = 0.75 / (nel_1d * order ** 2)
    elif dim == 3:
        # no deep meaning here, just a fudge factor
        dt = 0.45 / (nel_1d * order ** 2)
    else:
        raise ValueError("don't have a stable time step guesstimate")

    print("%d elements" % mesh.nelements)

    discr = EagerDGDiscretization(actx, mesh, order=order)

    fields = thaw(actx, freeze(flat_obj_array(
                  bump(actx, discr),
                  [discr.zeros(actx) for i in range(discr.dim)]
                  )))

    # vis = make_visualizer(discr, order + 3 if dim == 2 else order)

    def rhs(t, w):
        return wave_operator(discr, c=1, w=w)

    compiled_rhs = actx.compile(lambda y: rk4_step(y, 0, dt, rhs), fields)

    t = 0
    t_final = 1
    istep = 0
    while t < t_final:
        fields = compiled_rhs(fields)

        print(istep, t, la.norm(actx.to_numpy(fields[0][0])))

        t += dt
        istep += 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Wave-lazy (non-MPI version)")
    parser.add_argument("--profile", action="store_true",
        help="enable kernel profiling")
    args = parser.parse_args()

    main(use_profiling=args.profile)

# vim: foldmethod=marker