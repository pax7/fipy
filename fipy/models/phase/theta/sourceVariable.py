#!/usr/bin/env python

## -*-Pyth-*-
 # ###################################################################
 #  FiPy - Python-based finite volume PDE solver
 # 
 #  FILE: "sourceVariable.py"
 #                                    created: 11/12/03 {10:39:23 AM} 
 #                                last update: 9/3/04 {10:38:57 PM}
 #  Author: Jonathan Guyer <guyer@nist.gov>
 #  Author: Daniel Wheeler <daniel.wheeler@nist.gov>
 #  Author: James Warren   <jwarren@nist.gov>
 #    mail: NIST
 #     www: http://www.ctcms.nist.gov/fipy/
 #  
 # ========================================================================
 # This software was developed at the National Institute of Standards
 # and Technology by employees of the Federal Government in the course
 # of their official duties.  Pursuant to title 17 Section 105 of the
 # United States Code this software is not subject to copyright
 # protection and is in the public domain.  FiPy is an experimental
 # system.  NIST assumes no responsibility whatsoever for its use by
 # other parties, and makes no guarantees, expressed or implied, about
 # its quality, reliability, or any other characteristic.  We would
 # appreciate acknowledgement if the software is used.
 # 
 # This software can be redistributed and/or modified freely
 # provided that any derivative works bear some notice that they are
 # derived from it, and any modified versions bear some notice that
 # they have been modified.
 # ========================================================================
 #  
 #  Description: 
 # 
 #  History
 # 
 #  modified   by  rev reason
 #  ---------- --- --- -----------
 #  2003-11-12 JEG 1.0 original
 # ###################################################################
 ##

import Numeric

from fipy.variables.cellVariable import CellVariable
from fipy.tools.inline import inline
from fipy.models.phase.phase.addOverFacesVariable import AddOverFacesVariable
from noModularVariable import NoModularVariable

class SourceVariable(CellVariable):

    def __init__(self,
                 phase = None,
                 theta = None,
                 diffCoeff = None,
                 halfAngleVariable = None,
                 parameters = None):

        CellVariable.__init__(self, theta.getMesh(), hasOld = 0)
        
        self.parameters = parameters
        self.phase = self.requires(phase)
        self.theta = self.requires(theta)
        self.diffCoeff = self.requires(diffCoeff)
        self.halfAngleVariable = self.requires(halfAngleVariable)
        self.thetaNoMod = NoModularVariable(self.theta)        
        thetaGradDiff = self.theta.getFaceGrad() - self.thetaNoMod.getFaceGrad()
        self.AOFVariable = AddOverFacesVariable(faceGradient = thetaGradDiff, faceVariable = self.diffCoeff)

    def _calcValue(self):
        inline.optionalInline(self._calcValueInline, self._calcValuePy)

    def _calcValueInline(self):

        inline.runInlineLoop1("""
        halfAngleSq = halfAngleVariable(i) * halfAngleVariable(i);
        beta = (1. - halfAngleSq) / (1. + halfAngleSq);
        dbeta = symmetry * 2. * halfAngleVariable(i) / (1. + halfAngleSq);
        value(i) = AOFVariable(i) + alpha * alpha * c2 * dbeta * phaseGradMag(i) * (1. + c2 * beta);""",halfAngleSq = 0.,
                              halfAngleVariable =  self.halfAngleVariable.getNumericValue(),
                              beta = 0.,
                              dbeta = 0.,
                              symmetry = self.parameters['symmetry'],
                              value = self._getArray(),
                              AOFVariable = self.AOFVariable.getNumericValue(),
                              alpha = self.parameters['alpha'],
                              c2 = self.parameters['anisotropy'],
                              phaseGradMag = self.phase.getGrad().getMag().getNumericValue(),
                              ni = len(self.phase.getNumericValue()))
                              
    def _calcValuePy(self):

        mesh = self.theta.getMesh()
        c2 = self.parameters['anisotropy']

        halfAngleSq = self.halfAngleVariable[:] * self.halfAngleVariable[:]
        beta = (1. - halfAngleSq) / (1. + halfAngleSq)
        dbeta = self.parameters['symmetry'] * 2. * self.halfAngleVariable[:] / (1. + halfAngleSq)

        self.value = self.AOFVariable[:] + self.parameters['alpha']**2 * c2 * dbeta * self.phase.getGrad().getMag()[:] * (1. + c2 * beta)
        

