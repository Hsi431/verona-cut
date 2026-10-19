# Third-party dependencies

Verona's own source code is MIT licensed. That license does not replace the
licenses of its dependencies or grant rights to imported artwork.

| Dependency | Use | Upstream license |
| --- | --- | --- |
| [PySide6 / Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html) | Desktop interface, rendering and printing | LGPL/GPL/commercial alternatives; see the installed distribution and individual modules. |
| [svgpathtools](https://github.com/mathandy/svgpathtools/blob/master/LICENSE.txt) | SVG paths | MIT |
| [NumPy](https://numpy.org/doc/stable/license.html) | Geometry and raster processing | BSD terms plus bundled component notices |
| [SciPy](https://github.com/scipy/scipy/blob/main/LICENSE.txt) | Printed bleed color extension | BSD terms plus bundled component notices |
| [PyUSB](https://github.com/pyusb/pyusb/blob/master/LICENSE) | Experimental USB transport | BSD-3-Clause |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE) | Optional local automation | MIT |

The application imports QtCore, QtGui, QtWidgets, QtNetwork, QtPrintSupport,
QtPdf and QtSvg; tests also use QtTest. [Qt PDF](https://doc.qt.io/qt-6.10/qtpdf-index.html#licenses-and-attributions)
and [Qt SVG](https://doc.qt.io/qt-6.10/qtsvg-index.html#licenses-and-attributions)
provide LGPLv3/GPLv2/commercial alternatives with additional component notices.
Upstream Qt and Python distributions retain their own license texts. This
project does not modify Qt or bundle its libraries into its own wheel.

Transitive dependencies include Shiboken, svgwrite and, for MCP, Pydantic and
other packages. USB may use a separately installed [libusb](https://libusb.info/).
Retain the notices and satisfy the applicable terms when redistributing any
dependency, especially in a bundled executable. This table is not a complete
transitive bill of materials.

Example artwork and the two SVG chevrons in this source tree were created for
the project. No vendor application, SDK, firmware, fonts or extracted visual
assets are included. Compatibility names do not imply affiliation or endorsement.
