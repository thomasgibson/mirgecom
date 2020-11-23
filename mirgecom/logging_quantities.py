"""Support for time series logging."""

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

__doc__ = """
.. autoclass:: StateConsumer
.. autoclass:: DiscretizationBasedQuantity
.. autoclass:: ConservedDiscretizationBasedQuantity
.. autoclass:: DependentDiscretizationBasedQuantity
.. autoclass:: KernelProfile
.. autofunction:: add_package_versions
.. autofunction:: set_state
"""

from logpyle import LogQuantity, LogManager
from numpy import ndarray
from meshmode.array_context import PyOpenCLArrayContext
from meshmode.discretization import Discretization
from mirgecom.eos import GasEOS
from meshmode.dof_array import DOFArray
import pyopencl as cl


# {{{ Package versions

def add_package_versions(mgr: LogManager, path_to_version_sh: str = None) -> None:
    """Add the output of the emirge version.sh script to the log.

    Parameters
    ----------
    mgr
        The :class:LogManager to add the versions to.

    path_to_version_sh
        Path to emirge's version.sh script. The function will attempt to find this
        script automatically if this argument is not specified.

    """
    import subprocess
    from warnings import warn

    # Find emirge's version.sh in any parent directory
    if path_to_version_sh is None:
        import pathlib
        p = pathlib.Path(".").resolve()
        for d in p.parents:
            candidate = pathlib.Path(d).joinpath("version.sh")
            if candidate.is_file():
                with open(candidate) as f:
                    if "emirge" in f.read():
                        path_to_version_sh = str(candidate)
                        break

    if path_to_version_sh is None:
        output = "Could not find emirge's version.sh. No package versions recorded."
        warn(output)

    else:
        try:
            output = subprocess.check_output(path_to_version_sh)
        except OSError:
            output = "Could not record emirge's package versions."
            warn(output)

    mgr.set_constant("emirge_package_versions", output)

# }}}


# {{{ Device name

def add_device_name(mgr: LogManager, queue: cl.CommandQueue) -> None:
    """Add the device name to the log."""

    mgr.set_constant("device_name", str(queue.get_info(cl.command_queue_info.DEVICE)))


# }}}

def set_state(mgr: LogManager, state: ndarray) -> None:
    """Update the state of all :class:`StateConsumer` of the log manager `mgr`.

    Parameters
    ----------
    mgr
        The :class:LogManager to set the state of.

    state
        The state vector to the set the state to.
    """
    for gd_lst in [mgr.before_gather_descriptors,
            mgr.after_gather_descriptors]:
        for gd in gd_lst:
            if isinstance(gd.quantity, StateConsumer):
                gd.quantity.set_state(state)


class StateConsumer:
    """Base class for quantities that require a state for logging."""

    def __init__(self):
        self.state = None

    def set_state(self, state: ndarray) -> None:
        """Set the state of the object."""
        self.state = state

# {{{ Discretization-based quantities


class DiscretizationBasedQuantity(LogQuantity, StateConsumer):
    """Logging support for physical quantities."""

    def __init__(self, discr: Discretization, quantity: str, unit: str, op: str,
                 name: str):

        LogQuantity.__init__(self, name, unit)
        StateConsumer.__init__(self)

        self.discr = discr

        self.quantity = quantity

        from functools import partial

        if op == "min":
            self._discr_reduction = partial(self.discr.nodal_min, "vol")
            self.rank_aggr = min
        elif op == "max":
            self._discr_reduction = partial(self.discr.nodal_max, "vol")
            self.rank_aggr = max
        elif op == "sum":
            self._discr_reduction = partial(self.discr.nodal_sum, "vol")
            self.rank_aggr = sum
        else:
            raise RuntimeError(f"unknown operation {op}")

    @property
    def default_aggregator(self):
        """Rank aggregator to use."""
        return self.rank_aggr

    def __call__(self):
        """Return the requested quantity."""
        raise NotImplementedError


class ConservedDiscretizationBasedQuantity(DiscretizationBasedQuantity):
    """Logging support for conserved quantities.

    See :meth:`~mirgecom.euler.split_conserved` for details.
    """

    def __init__(self, discr: Discretization, quantity: str, op: str,
                 unit: str = None, dim: int = None, name: str = None):
        if unit is None:
            from warnings import warn
            if quantity == "mass":
                unit = "kg/m^3"
            elif quantity == "energy":
                unit = "J/m^3"
            elif quantity == "momentum":
                if dim is None:
                    raise RuntimeError("Missing 'dim' parameter for dimensional "
                                       f"ConservedQuantity '{quantity}'.")
                unit = "kg*m/s/m^3"
            else:
                unit = ""
            warn(f"Inferred unit for quantity {quantity} : {unit}")

        if name is None:
            name = f"{op}_{quantity}" + (str(dim) if dim is not None else "")

        super().__init__(discr, quantity, unit, op, name)

        self.dim = dim

    def __call__(self):
        """Return the requested conserved quantity."""
        if self.state is None:
            return None

        from mirgecom.euler import split_conserved

        cv = split_conserved(self.discr.dim, self.state)
        self.state = None
        cq = getattr(cv, self.quantity)

        if not isinstance(cq, DOFArray):
            return self._discr_reduction(cq[self.dim])
        else:
            return self._discr_reduction(cq)


class DependentDiscretizationBasedQuantity(DiscretizationBasedQuantity):
    """Logging support for dependent quantities (temperature, pressure)."""

    def __init__(self, discr: Discretization, eos: GasEOS,
                 quantity: str, op: str, unit: str = None, name: str = None):
        if unit is None:
            from warnings import warn
            if quantity == "temperature":
                unit = "K"
            elif quantity == "pressure":
                unit = "P"
            else:
                unit = ""
            warn(f"Inferred unit for quantity {quantity} : {unit}")

        if name is None:
            name = f"{op}_{quantity}"

        super().__init__(discr, quantity, unit, op, name)

        self.eos = eos

    def __call__(self):
        """Return the requested dependent quantity."""
        if self.state is None:
            return None

        from mirgecom.euler import split_conserved

        cv = split_conserved(self.discr.dim, self.state)
        dv = self.eos.dependent_vars(cv)

        self.state = None

        return self._discr_reduction(getattr(dv, self.quantity))

# }}}

# {{{ Kernel profile quantities


class KernelProfile(LogQuantity):
    """Logging support for statistics of the OpenCL kernel profiling (time, \
    num_calls, flops, bytes_accessed).

    Parameters
    ----------
    actx
        The array context from which to collect statistics. Must have profiling
        enabled.

    kernel_name
        Name of the kernel to profile.

    stat
        Statistic to collect (can be one of "time", "num_calls", "flops",
        "bytes_accessed").

    name
        Name under which the statistic will be stored (optional).

    """

    def __init__(self, actx: PyOpenCLArrayContext,
                 kernel_name: str, stat: str, name: str = None):
        if stat == "time":
            unit = "s"
        else:
            unit = ""

        if name is None:
            name = f"{kernel_name}_{stat}"

        super().__init__(name, unit, f"{stat} of '{kernel_name}'")

        self.kernel_name = kernel_name
        self.actx = actx
        self.stat = stat

    def __call__(self):
        """Return the requested quantity."""
        return(self.actx.get_profiling_data_for_kernel(self.kernel_name, self.stat))

# }}}