#!/usr/bin/env python
 
## -*-Pyth-*-
# ###################################################################
#  FiPy - a finite volume PDE solver in Python
#
#  FILE: "gmshImport.py"
#
#  Author: James O'Beirne <james.obeirne@nist.gov>
#  Author: Jonathan Guyer <guyer@nist.gov>
#  Author: Daniel Wheeler <daniel.wheeler@nist.gov>
#  Author: James Warren   <jwarren@nist.gov>
#    mail: NIST
#     www: http://www.ctcms.nist.gov/fipy/
# 
# ========================================================================
# This document was prepared at the National Institute of Standards
# and Technology by employees of the Federal Government in the course
# of their official duties.  Pursuant to title 17 Section 105 of the
# United States Code this document is not subject to copyright
# protection and is in the public domain.  gmshExport.py
# is an experimental work.  NIST assumes no responsibility whatsoever
# for its use by other parties, and makes no guarantees, expressed
# or implied, about its quality, reliability, or any other characteristic.
# We would appreciate acknowledgement if the document is used.
#
# This document can be redistributed and/or modified freely
# provided that any derivative works bear some notice that they are
# derived from it, and any modified versions bear some notice that
# they have been modified.
# ========================================================================
#  See the file "license.terms" for information on usage and
#  redistribution
#  of this file, and for a DISCLAIMER OF ALL WARRANTIES.
# 
# ###################################################################
##

__docformat__ = 'restructuredtext'

from fipy.tools import numerix as nx
from fipy.tools import parallel
import mesh
import mesh2D
import os
import tempfile

class MshFile:
    """
    Class responsible for parsing a Gmsh file and then readying
    its contents for use by a `Mesh` constructor.

    Does not support gmsh versions < 2.
    """
    def __init__(self, filename, dimensions, coordDimensions=None):
        """
        Isolates relevant data into two files, stores in 
        `self.nodesFile` for $Nodes,
        `self.elemsFile` for $Elements.

        :Parameters:
          - `filename`: a string indicating gmsh output file
          - `dimensions`: an integer indicating dimension of mesh
          - `coordDimension`: an integer indicating dimension of shapes
        """
        
        self.coordDimensions = coordDimensions or dimensions
        self.dimensions      = dimensions
        gmshFlags            = self._prepareGmshFlags()
        self.filename        = self._parseFilename(filename, gmshFlags)

        # we need a conditional here so we don't pick up 2D shapes in 3D
        if dimensions == 2: 
            self.numFacesForShape = {2: 3, # triangle:   3 sides
                                     3: 4} # quadrangle: 4 sides
        else: # 3D
            self.numFacesForShape = {4: 4} # tet:        4 sides

        f = open(self.filename, "r") # open the msh file

        self.version, self.fileType, self.dataSize = self._getMetaData(f)
        self.nodesFile = self._isolateData("Nodes", f)
        self.elemsFile = self._isolateData("Elements", f)

        self.vertexCoords, \
        self.facesToV, \
        self.cellsToF = self._buildMeshInformation()

    def _buildMeshInformation(self):
        """
        Removed from __init__ to decouple order of mesh building. We'll
        override this when we subclass `MshFile` in `PartedMshFile`, and
        we'll build up the mesh in a different order so we don't have to
        keep all of the vertices around in memory.

        This may be harder to follow than the original equivalent, but I 
        think the gained modularity will pay off.
        """
        vertexCoords, vertexMap = self._vertexCoordsAndMap()

        cellsToVertGmshIDs, \
        shapeTypes, \
        numCells = self._parseElementFile()

        # translate Gmsh IDs to vertexCoord indices
        cellsToVertIDs = self._translateVertIDToIdx(cellsToVertGmshIDs,
                                                    vertexMap)
        # strip out the extra padding in `shapeTypes`
        shapeTypes = nx.delete(shapeTypes,  nx.s_[numCells:])
         
        facesToV, cellsToF = self._deriveCellsAndFaces(cellsToVertIDs,
                                                       shapeTypes,
                                                       numCells)
        return vertexCoords, facesToV, cellsToF
 
    def _prepareGmshFlags(self):
        """
        Another separation from __init__ for the purposes of redefinition
        in `PartedMshFile`.
        """
        return "-%d -v 0 -format msh" % self.dimensions
 
    def _parseFilename(self, fname, gmshFlags):
        """
        If we're being passed a .msh file, leave it be. Otherwise,
        we've gotta compile a .msh file from either (i) a .geo file, 
        or (ii) a gmsh script passed as a string.
        """
        lowerFname = fname.lower()
        if '.msh' in lowerFname:
            return fname
        else:
            if '.geo' in lowerFname or '.gmsh' in lowerFname:
                geoFile = fname
            else: # fname must be a full script, not a file
                (f, geoFile) = tempfile.mkstemp('.geo')
                file = open(geoFile, 'w')
                file.writelines(fname)
                file.close(); os.close(f)

            (f, mshFile) = tempfile.mkstemp('.msh')
            os.system('gmsh %s %s -o %s' \
                      % (geoFile, gmshFlags, mshFile))
            os.close(f)

            return mshFile
         
    def _getMetaData(self, f):
        """
        Extracts gmshVersion, file-type, and data-size in that
        order.
        """
        self._seekForHeader("MeshFormat", f)
        metaData = f.readline().split()
        f.seek(0)
        return [float(x) for x in metaData]

    def _isolateData(self, title, f):
        """
        Gets all data between $[title] and $End[title], writes
        it out to its own file.
        """
        newF = tempfile.TemporaryFile()
        self._seekForHeader(title, f)
        
        # extract the actual data within section
        while True:
            line = f.readline()
            if ("$End%s" % title) not in line: newF.write(line) 
            else: break

        f.seek(0); newF.seek(0) # restore file positions
        return newF

    def _seekForHeader(self, title, f):
        """
        Iterate through a file until we end up at the section header
        for `title`. Function has obvious side-effects on `f`.
        """
        while True:
            line = f.readline()
            if len(line) == 0:
                raise EOFError("No `%s' header found!" % title)
                break
            elif (("$%s" % title) not in line): continue
            else: break # found header

    def _vertexCoordsAndMap(self):
        """
        Extract vertex coordinates and mapping information from
        nx.genfromtxt-friendly file, generated in `_isolateData`.

        Returns both the vertex coordinates and the mapping information.
        Mapping information is stored in a 1xn array where n is the
        largest vertexID obtained from the gmesh file. This mapping
        array is later used to transform element information.
        """
        # dtype
        gen = nx.genfromtxt(fname=self.nodesFile, skiprows=1)
        self.nodesFile.close()

        vertexCoords = gen[:, 1:] # strip out column 0
        vertexIDs    = gen[:, :1].flatten().astype(int)
        
        # `vertexToIdx`: gmsh-vertex ID -> `vertexCoords` index
        vertexToIdx = nx.empty(vertexIDs.max() + 1, dtype=int)
        vertexToIdx[vertexIDs] = nx.arange(len(vertexIDs))

        # transpose for FiPy, truncate for dimension
        return vertexCoords.transpose()[:self.coordDimensions], vertexToIdx

    def _deriveCellsAndFaces(self, cellsToVertIDs, shapeTypes, numCells):
        """
        Uses element information obtained from `_parseElementFile` to deliver
        `facesToVertices` and `cellsToFaces`.
        """

        def formatForFiPy(arr): return arr.swapaxes(0,1)[::-1]

        allShapes  = nx.unique(shapeTypes).tolist()
        maxFaces   = max([self.numFacesForShape[x] for x in allShapes])

        # `cellsToFaces` must be padded with -1; see mesh.py
        faceLength   = self.dimensions
        currNumFaces = 0
        cellsToFaces = nx.ones((numCells, maxFaces)) * -1
        facesDict    = {}
        uniqueFaces  = []

        # we now build `cellsToFaces` and `uniqueFaces`,
        # the latter will result in `facesToVertices`.
        for cellIdx in range(numCells):
            cell         = cellsToVertIDs[cellIdx]
            facesPerCell = self.numFacesForShape[shapeTypes[cellIdx]]
            faces        = self._extractFaces(faceLength, facesPerCell, cell)

            for faceIdx in range(facesPerCell):
                # NB: currFace is sorted for the key to spot duplicates
                currFace = faces[faceIdx]
                keyStr   = ' '.join([str(x) for x in sorted(currFace)])

                if facesDict.has_key(keyStr):
                    cellsToFaces[cellIdx][faceIdx] = facesDict[keyStr]
                else: # new face
                    facesDict[keyStr] = currNumFaces
                    cellsToFaces[cellIdx][faceIdx] = currNumFaces
                    uniqueFaces.append(currFace)
                    currNumFaces += 1

        facesToVertices = nx.array(uniqueFaces, dtype=int)

        return formatForFiPy(facesToVertices), formatForFiPy(cellsToFaces)
 
    def _parseElementFile(self):
        """
        Returns `cellsToVertIDs`, which maps cells to Gmsh-given IDs;
                `shapeTypes`, which maps each cell to a shapeType;
                `numCells`, the number of cells to be processed.
        """
        
        # shapeTypes:  cellsToVertIDs index -> shape type id
        els            = self.elemsFile.readlines()
        cellsToVertIDs = []
        shapeTypes     = nx.empty(len(els), dtype=int)
        numCells       = 0

        # read in Elements data from gmsh
        for element in els[1:]: # skip number-of-elems line
            currLineInts = [int(x) for x in element.split()]
            elemType     = currLineInts[1]
            numTags      = currLineInts[2]

            if elemType in self.numFacesForShape.keys():
                # NB: 3 columns precede the tags
                cellsToVertIDs.append(currLineInts[(3+numTags):])
                shapeTypes[numCells] = elemType # record shape type
                numCells += 1
            else:
                continue # shape not recognized

        self.elemsFile.close() # tempfile trashed

        return cellsToVertIDs, shapeTypes, numCells
                                                     
    def _translateVertIDToIdx(self, cellsToVertIDs, vertexMap):
        """
        Translates cellToIds from Gmsh output IDs to `vertexCoords`
        indices.
        """
        cellsToVertIdxs = []

        # translate gmsh vertex IDs to vertexCoords indices
        for cell in cellsToVertIDs:
            vertIndices = vertexMap[nx.array(cell)]
            cellsToVertIdxs.append(vertIndices)

        return cellsToVertIdxs
     
    def _extractFaces(self, faceLen, facesPerCell, cell):
        """
        Given `cell`, a cell in terms of vertices, returns an array of
        `facesPerCell` faces of length `faceLen` in terms of vertices.
        """
        faces = []
        for i in range(facesPerCell):
            aFace = []
            for j in range(faceLen):
                aVertex = (i + j) % len(cell) # we may wrap
                aFace.append(int(cell[aVertex]))
            faces.append(aFace)
        return faces
         
class PartedMshFile(MshFile):
    """
    Gmsh version must be >= 2.5.

    Same `__init__` as `MshFile`. `_buildMeshInformation` fulfills same
    contract as parent's version.
    """
    def _buildMeshInformation(self):
        cellDataDict, ghostCellDataDict = self._parseElementFile()
        
    def _vertexCoordsAndMap(self):
        pass

    def _prepareGmshFlags(self):
        return "-%d -v 0 -part %d -format msh" % (self.dimensions,
                                                  parallel.Nproc)

    def _parseElementFile(self):
        """
        Returns two dicts, the first for non-ghost cells and the second for
        ghost cells. Each dict contains 
            "verts": A cellToVertID Python array,
            "shapes": A shapeTypes Python array,
            "num": A scalar, number of cells
            "idmap": A Python array which maps vertexCoords idx -> global ID

        It's pretty nasty.
        """
        def _addCell(cellArr, currLine, shapeTs, elT, IDmap, IDoff):
            """
            Bookkeeping for cells. Declared as own function for generality.
            Curried later in ghost/non-ghost specific lambdas.
            Just full of side-effects. Sorry, Abelson/Sussman.
            """
            cellArr.append(currLine[(numTags+3):])
            shapeTs.append(elT)
            IDmap.append(currLine[0] - IDoff)

        cellsToVertIDs  = []
        gCellsToVertIDs = [] # ghosts
        shapeTypes      = []
        gShapeTypes     = [] # ghosts, again
        numCells        = 0
        numGhostCells   = 0
        vertIDMap       = [] # vertexCoords idx -> gmsh ID (global ID)
        ghostVertIDMap  = [] # vertexCoords idx -> gmsh ID (global ID)
        IDOffset        = -1 # this will be subtractd from gmsh ID to obtain
                             # global ID

        #pid             = parallel.procID
        pid             = 3 # for testing

        # thank god Python doesn't do closures like Lisp. Curry, curry, curry.
        addCell      = lambda c,e: _addCell(cellsToVertIDs, c,
                                            shapeTypes, e, 
                                            vertIDMap, IDOffset)
        addGhostCell = lambda c,e: _addCell(gCellsToVertIDs, c,
                                            gShapeTypes, e, 
                                            ghostVertIDMap, IDOffset)

        self.elemsFile.readline() # skip number of elements
        # the following iteration construct doesn't read self.elemsFile 
        # into memory in its entirety; I checked with strace
        for el in self.elemsFile:
            currLineInts = [int(x) for x in el.split()]
            elemType     = currLineInts[1]

            if elemType in self.numFacesForShape.keys():
                if IDOffset == -1: # if first valid shape
                    IDOffset = currLineInts[0] # establish offset

                numTags   = currLineInts[2]
                partID    = currLineInts[3+numTags-1]

                if partID == pid: # el is in this processor's partition
                    addCell(currLineInts, elemType)
                    numCells += 1
                elif partID < 0:  # el is a boundary cell
                    realPartID = currLineInts[3+numTags-2]
                    # ...current partID is actually ghost cell ID

                    if (partID * -1) == pid: # el is a ghost cell for this part.
                        addGhostCell(currLineInts, elemType)
                        numGhostCells += 1
                    elif realPartID == pid: # a boundary cell in this part.
                        addCell(currLineInts, elemType)
                        numCells += 1

        self.elemsFile.close() # tempfile trashed

        # first dict is non-ghost, second is ghost
        return {'verts':  cellsToVertIDs, 
                'shapes': shapeTypes, 
                'num':    numCells,
                'idmap':  vertIDMap}, \
               {'verts':  gCellsToVertIDs, 
                'shapes': gShapeTypes, 
                'num':    numGhostCells,
                'idmap':  ghostVertIDMap}


class Gmsh2D(mesh2D.Mesh2D):
    def __init__(self, arg, coordDimensions=2):
        self.mshFile = MshFile(arg, dimensions=2, 
                               coordDimensions=coordDimensions)
        self.verts   = self.mshFile.vertexCoords
        self.faces   = self.mshFile.facesToV
        self.cells   = self.mshFile.cellsToF
        mesh2D.Mesh2D.__init__(self, vertexCoords=self.verts,
                                     faceVertexIDs=self.faces,
                                     cellFaceIDs=self.cells)

    def getCellVolumes(self):
        return abs(mesh2D.Mesh2D.getCellVolumes(self))

    def _test(self):
        """
        First, we'll test GmshImporter2D on a small circle with triangular
        cells.

        >>> circ = Gmsh2D('''
        ... cellSize = 1; 
        ... radius   = 0.25; 
        ... Point(1) = {0, 0, 0, cellSize}; 
        ... Point(2) = {-radius, 0, 0, cellSize}; 
        ... Point(3) = {0, radius, 0, cellSize}; 
        ... Point(4) = {radius, 0, 0, cellSize}; 
        ... Point(5) = {0, -radius, 0, cellSize}; 
        ... Circle(6) = {2, 1, 3}; 
        ... Circle(7) = {3, 1, 4}; 
        ... Circle(8) = {4, 1, 5}; 
        ... Circle(9) = {5, 1, 2}; 
        ... Line Loop(10) = {6, 7, 8, 9}; 
        ... Plane Surface(11) = {10}; 
        ... ''')

        >>> print circ.getVertexCoords()
        [[ 0.         -0.25        0.          0.25        0.         -0.1767767
           0.1767767   0.1767767  -0.1767767  -0.0654061   0.08758673 -0.0379699 ]
         [ 0.          0.          0.25        0.         -0.25        0.1767767
           0.1767767  -0.1767767  -0.1767767   0.0654061   0.03383883 -0.08405141]]

        >>> print circ._getCellFaceIDs()
        [[ 2  1  7 10  6 13 15 14 13 19 21 17]
         [ 1  4  6  9 12 11 10 16 16 18 20 20]
         [ 0  3  5  8 11  4 14  2 17  5  8 19]]

        >>> print circ.getCellVolumes()[0] > 0
        True

        Now we'll test GmshImporter2D again, but on a rectangle.

        >>> rect = Gmsh2D('''
        ... cellSize = 0.5;
        ... radius   = 10;
        ... Point(2) = {-radius, radius, 0, cellSize};
        ... Point(3) = {radius, radius, 0, cellSize};
        ... Point(4) = {radius, -radius, 0, cellSize};
        ... Point(5) = {-radius, -radius, 0, cellSize};
        ... Line(6) = {2, 3};
        ... Line(7) = {3, 4};
        ... Line(8) = {4, 5};
        ... Line(9) = {5, 2};
        ... Line Loop(10) = {6, 7, 8, 9};
        ... Plane Surface(11) = {10};
        ... ''')

        >>> print rect.getCellVolumes()[0] > 0
        True

        >>> print rect._getFaceVertexIDs()
        [[ 274  270  187 ..., 1008 1008   27]
         [ 187  274  270 ...,   26   27   26]]

        >>> print rect.getFaceCenters()
        [[ -8.05265841  -7.98540272  -7.79914904 ...,   1.84932005   2.10573031
            2.05128205]
         [ -8.93930664  -8.62975505  -8.83201622 ...,   9.67912179   9.67912179
           10.        ]]

        Testing multiple shape types within a mesh;

        >>> circle = Gmsh2D('''
        ... cellSize = 0.05;
        ... radius = 1;
        ... Point(1) = {0, 0, 0, cellSize};
        ... Point(2) = {-radius, 0, 0, cellSize};
        ... Point(3) = {0, radius, 0, cellSize};
        ... Point(4) = {radius, 0, 0, cellSize};
        ... Point(5) = {0, -radius, 0, cellSize};
        ... Circle(6) = {2, 1, 3};
        ... Circle(7) = {3, 1, 4};
        ... Circle(8) = {4, 1, 5};
        ... Circle(9) = {5, 1, 2};
        ... Line Loop(10) = {6, 7, 8, 9};
        ... Plane Surface(11) = {10};
        ... Recombine Surface{11};
        ... ''')

        >>> print circle.getCellVolumes()[0] > 0
        True
        """

class Gmsh2DIn3DSpace(Gmsh2D):
    def __init__(self, arg):
        Gmsh2D.__init__(self, arg, coordDimensions=3)

    def _test(self):
        """
        Stolen from the cahnHilliard sphere example.

        >>> sphere = Gmsh2DIn3DSpace('''
        ... radius = 5.0;
        ... cellSize = 0.3;
        ...
        ... // create inner 1/8 shell
        ... Point(1) = {0, 0, 0, cellSize};
        ... Point(2) = {-radius, 0, 0, cellSize};
        ... Point(3) = {0, radius, 0, cellSize};
        ... Point(4) = {0, 0, radius, cellSize};
        ... Circle(1) = {2, 1, 3};
        ... Circle(2) = {4, 1, 2};
        ... Circle(3) = {4, 1, 3};
        ... Line Loop(1) = {1, -3, 2} ;
        ... Ruled Surface(1) = {1};
        ...
        ... // create remaining 7/8 inner shells
        ... t1[] = Rotate {{0,0,1},{0,0,0},Pi/2}
        ... {Duplicata{Surface{1};}};
        ... t2[] = Rotate {{0,0,1},{0,0,0},Pi}
        ... {Duplicata{Surface{1};}};
        ... t3[] = Rotate {{0,0,1},{0,0,0},Pi*3/2}
        ... {Duplicata{Surface{1};}};
        ... t4[] = Rotate {{0,1,0},{0,0,0},-Pi/2}
        ... {Duplicata{Surface{1};}};
        ... t5[] = Rotate {{0,0,1},{0,0,0},Pi/2}
        ... {Duplicata{Surface{t4[0]};}};
        ... t6[] = Rotate {{0,0,1},{0,0,0},Pi}
        ... {Duplicata{Surface{t4[0]};}};
        ... t7[] = Rotate {{0,0,1},{0,0,0},Pi*3/2}
        ... {Duplicata{Surface{t4[0]};}};
        ...
        ... // create entire inner and outer shell
        ... Surface
        ... Loop(100)={1,t1[0],t2[0],t3[0],t7[0],t4[0],t5[0],t6[0]};
        ... ''').extrude(extrudeFunc=lambda r: 1.1 * r)

        >>> print sphere.getCellVolumes()[0] > 0
        True

        >>> print sphere.getCellCenters()
        [[-0.45068562 -0.45090581 -2.55590575 ...,  0.3046376   0.06084868
           0.12198487]
         [ 0.0875831   5.22669204  4.5798818  ...,  4.96201041  4.95750553
           4.90984865]
         [ 5.22670396  0.0874828   0.11050056 ..., -1.67683932 -1.71977404
          -1.85052202]]

        """

class Gmsh3D(mesh.Mesh):
    def __init__(self, arg):
        self.mshFile = MshFile(arg, dimensions=3)
        self.verts   = self.mshFile.vertexCoords
        self.faces   = self.mshFile.facesToV
        self.cells   = self.mshFile.cellsToF
        mesh.Mesh.__init__(self, vertexCoords=self.verts,
                                 faceVertexIDs=self.faces,
                                 cellFaceIDs=self.cells)

    def _test(self):
        """
        >>> prism = Gmsh3D('''
        ... cellSize = 0.5;
        ... Len = 2;
        ... Hei = 1;
        ... Wid = 1;
        ...
        ... Point(1) = {0, 0, 0, cellSize};
        ... Point(2) = {0, 0, Wid, cellSize};
        ... Point(3) = {0, Hei, Wid, cellSize};
        ... Point(4) = {0, Hei, 0, cellSize};
        ...
        ... Point(5) = {Len, 0, 0, cellSize};
        ... Point(6) = {Len, 0, Wid, cellSize};
        ... Point(7) = {Len, Hei, Wid, cellSize};
        ... Point(8) = {Len, Hei, 0, cellSize};
        ...
        ... Line(9)  = {1, 2};
        ... Line(10) = {2, 3};
        ... Line(11) = {3, 4};
        ... Line(12) = {4, 1};
        ...
        ... Line(13) = {5, 6};
        ... Line(14) = {6, 7};
        ... Line(15) = {7, 8};
        ... Line(16) = {8, 5};
        ...
        ... Line(17) = {1, 5};
        ... Line(18) = {2, 6};
        ... Line(19) = {3, 7};
        ... Line(20) = {4, 8};
        ...
        ... Line Loop(21) = {9, 10, 11, 12};
        ... Line Loop(22) = {13, 14, 15, 16};
        ... Line Loop(23) = {17, -16, -20, 12};
        ... Line Loop(24) = {13, -18, -9, 17};
        ... Line Loop(25) = {18, 14, -19, -10};
        ... Line Loop(26) = {-19, 11, 20, -15};
        ...
        ... Plane Surface(27) = {21};
        ... Plane Surface(28) = {22};
        ... Plane Surface(29) = {23};
        ... Plane Surface(30) = {24};
        ... Plane Surface(31) = {25};
        ... Plane Surface(32) = {26};
        ...
        ... Surface Loop(33) = {27, 28, 29, 30, 31, 32};
        ...
        ... Volume(34) = {33};
        ... ''')

        >>> print prism.getCellVolumes()[0] > 0
        True

        >>> print prism.getCellVolumes()
        [ 0.08333333  0.05555556  0.05555556  0.05555556  0.05555556  0.05555556
          0.02777778  0.02777778  0.02777778  0.05555556  0.05555556  0.02777778
          0.05555556  0.05555556  0.08333333  0.05555556  0.05555556  0.02777778
          0.02777778  0.05555556  0.05555556  0.05555556  0.05555556  0.05555556
          0.05555556  0.05555556  0.05555556  0.08333333  0.05555556  0.02777778
          0.02777778  0.05555556  0.05555556  0.05555556  0.02777778  0.08333333
          0.02777778  0.02777778  0.05555556  0.02777778]
        >>>
        """

def deprecation(old, new):
    import warnings
    warnings.warn("%s has been replaced by %s." % (old, new), 
                  DeprecationWarning, stacklevel=3)

class GmshImporter2D(Gmsh2D):
    def __init__(self, arg, coordDimensions=2):
        deprecation("GmshImporter2D", "Gmsh2D")
        Gmsh2D.__init__(self, arg, coordDimensions=coordDimensions)

class GmshImporter2DIn3DSpace(Gmsh2DIn3DSpace):
    def __init__(self, arg):
        deprecation("GmshImporter2DIn3DSpace", "Gmsh2DIn3DSpace")
        Gmsh2DIn3DSpace.__init__(self, arg)

class GmshImporter3D(Gmsh3D):
    def __init__(self, arg):
        deprecation("GmshImporter3D", "Gmsh3D")
        Gmsh3D.__init__(self, arg)
    
def _test():
    import doctest
    return doctest.testmod()

if __name__ == "__main__":
    _test()
