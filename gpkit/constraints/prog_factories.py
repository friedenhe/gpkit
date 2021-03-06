"Scripts for generating, solving and sweeping programs"
from __future__ import unicode_literals, print_function
from time import time
import numpy as np
from ..nomials import parse_subs
from ..solution_array import SolutionArray
from ..keydict import KeyDict
from ..small_scripts import maybe_flatten

try:
    from ad import adnumber
except ImportError:
    adnumber = None
    print("Couldn't import ad; automatic differentiation of linked variables"
          " is disabled.")


def evaluate_linked(constants, linked):
    "Evaluates the values and gradients of linked variables."
    if adnumber:
        kdc = KeyDict({k: adnumber(maybe_flatten(v))
                       for k, v in constants.items()})
        kdc.log_gets = True
    kdc_plain = None
    array_calulated, logged_array_gets = {}, {}
    for v, f in linked.items():
        try:
            assert adnumber  # trigger exit if ad not found
            if v.veckey and v.veckey.original_fn:
                if v.veckey not in array_calulated:
                    ofn = v.veckey.original_fn
                    array_calulated[v.veckey] = np.array(ofn(kdc))
                    logged_array_gets[v.veckey] = kdc.logged_gets
                logged_gets = logged_array_gets[v.veckey]
                out = array_calulated[v.veckey][v.idx]
            else:
                logged_gets = kdc.logged_gets
                out = f(kdc)
            constants[v] = out.x
            v.descr["gradients"] = {}
            for key in logged_gets:
                if key.shape:
                    grad = out.gradient(kdc[key])
                    v.gradients[key] = np.array(grad)
                else:
                    v.gradients[key] = out.d(kdc[key])
        except Exception:  # pylint: disable=broad-except
            from .. import settings
            if settings.get("ad_errors_raise", None):
                raise
            if adnumber:
                print("Couldn't auto-differentiate linked variable %s\n  "
                      "(to raise the error directly for debugging purposes,"
                      " set gpkit.settings[\"ad_errors_raise\"] to True)" % v)
            if kdc_plain is None:
                kdc_plain = KeyDict(constants)
            constants[v] = f(kdc_plain)
            v.descr.pop("gradients", None)
        finally:
            if adnumber:
                kdc.logged_gets = set()


def _progify_fctry(program, return_attr=None):
    """Generates function that returns a program() and optionally an attribute.

    Arguments
    ---------
    program: NomialData
        Class to return, e.g. GeometricProgram or SequentialGeometricProgram
    return_attr: string
        attribute to return in addition to the program
    """
    def programify(self, constants=None, **kwargs):
        "Return program version of self"
        if not constants:
            constants, _, linked = parse_subs(self.varkeys, self.substitutions)
            if linked:
                evaluate_linked(constants, linked)
        prog = program(self.cost, self, constants, **kwargs)
        if return_attr:
            return prog, getattr(prog, return_attr)
        return prog
    return programify


def _solve_fctry(genfunction):
    "Returns function for making/solving/sweeping a program."
    def solvefn(self, solver=None, verbosity=1, skipsweepfailures=False,
                **kwargs):
        """Forms a mathematical program and attempts to solve it.

         Arguments
         ---------
         solver : string or function (default None)
             If None, uses the default solver found in installation.
         verbosity : int (default 1)
             If greater than 0 prints runtime messages.
             Is decremented by one and then passed to programs.
         skipsweepfailures : bool (default False)
             If True, when a solve errors during a sweep, skip it.
         **kwargs : Passed to solver

         Returns
         -------
         sol : SolutionArray
             See the SolutionArray documentation for details.

         Raises
         ------
         ValueError if the program is invalid.
         RuntimeWarning if an error occurs in solving or parsing the solution.
         """
        constants, sweep, linked = parse_subs(self.varkeys, self.substitutions)
        solution = SolutionArray()

        # NOTE SIDE EFFECTS: self.program is set below
        if sweep:
            run_sweep(genfunction, self, solution, skipsweepfailures,
                      constants, sweep, linked,
                      solver, verbosity, **kwargs)
        else:
            self.program, progsolve = genfunction(self)
            result = progsolve(solver, verbosity, **kwargs)
            solution.append(result)
        solution.program = self.program
        solution.model = self
        solution.to_arrays()
        if self.cost.units:
            solution["cost"] = solution["cost"] * self.cost.units
        self.solution = solution  # NOTE: SIDE EFFECTS
        return solution
    return solvefn


# pylint: disable=too-many-locals,too-many-arguments
def run_sweep(genfunction, self, solution, skipsweepfailures,
              constants, sweep, linked,
              solver, verbosity, **kwargs):
    "Runs through a sweep."
    # sort sweeps by the eqstr of their varkey
    sweepvars, sweepvals = zip(*sorted(list(sweep.items()),
                                       key=lambda vkval: vkval[0].eqstr))
    if len(sweep) == 1:
        sweep_grids = np.array(list(sweepvals))
    else:
        sweep_grids = np.meshgrid(*list(sweepvals))

    N_passes = sweep_grids[0].size
    sweep_vects = {var: grid.reshape(N_passes)
                   for (var, grid) in zip(sweepvars, sweep_grids)}

    if verbosity > 0:
        print("Solving over %i passes." % N_passes)
        tic = time()

    self.program = []
    for i in range(N_passes):
        constants.update({var: sweep_vect[i]
                          for (var, sweep_vect) in sweep_vects.items()})
        if linked:
            kdc = KeyDict(constants)
            constants.update({v: f(kdc) for v, f in linked.items()})
        program, solvefn = genfunction(self, constants)
        self.program.append(program)  # NOTE: SIDE EFFECTS
        try:
            solution.append(solvefn(solver, verbosity-1, **kwargs))
        except (RuntimeWarning, ValueError):
            if not skipsweepfailures:
                raise RuntimeWarning("solve failed during sweep; program"
                                     " has been saved to m.program[-1]."
                                     " To ignore such failures, solve with"
                                     " skipsweepfailures=True.")
    if not solution:
        raise RuntimeWarning("no sweeps solved successfully.")

    solution["sweepvariables"] = KeyDict()
    ksweep = KeyDict(sweep)
    for var, val in list(solution["constants"].items()):
        if var in ksweep:
            solution["sweepvariables"][var] = val
            del solution["constants"][var]
        elif var not in linked:
            solution["constants"][var] = [val[0]]

    if verbosity > 0:
        soltime = time() - tic
        print("Sweeping took %.3g seconds." % (soltime,))
