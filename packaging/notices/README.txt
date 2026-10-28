Supplementary license texts
===========================

The GNU LGPLv3, GPLv3 and GPLv2 texts in this directory are verbatim copies
from Qt's upstream qtbase v6.8.0 LICENSES directory. They supplement the
PySide6/Shiboken wheel metadata, which may declare an open-source license
expression while shipping only its commercial-license reference text.
These standard license texts do not depend on the version of the Qt binary.

SOURCES.json records the exact source URLs and SHA-256 hashes. They are
checked into this project so normal application builds do not need network
access to obtain license text.

scripts/build.py copies the installed runtime distributions' available
license files, their versions and source locations, this directory, the
application MIT license, Python's license, and PyInstaller's bootloader
exception into each distributable. The generated inventory.json records
the actual files and hashes used by that build. This does not certify the
completeness of an upstream package's license inventory.

Upstream Qt component inventories and source archives:
https://doc.qt.io/qt-6/licenses-used-in-qt.html
https://doc.qt.io/qtforpython-6/licenses.html
https://download.qt.io/archive/qt/
