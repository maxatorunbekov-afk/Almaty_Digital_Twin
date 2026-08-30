## Software Requirements

The following software is required to fully reproduce the experimental software and control environment:

* Windows 10 or Windows 11, 64-bit;
* Fluidit Heat 2.8 with a valid license for the hydraulic and thermal simulation of the district heating network;
* Siemens TIA Portal V20 with STEP 7 Professional for opening and modifying the PLC program;
* WinCC Advanced or WinCC Professional for opening and running the SCADA/HMI project;
* S7-PLCSIM Advanced for reproducing the experiment with a virtual PLC. This component is not required when a physical PLC is used;
* Python 3.11 or later;
* the `asyncua` library for OPC UA communication between Python and the PLC;
* AnyLogic PLE 8.9.7 for opening and running the supplementary district heating simulation model.

The main Python dependencies can be installed using:

`pip install -r requirements.txt`

The `json` module is included in the Python standard library and does not require separate installation.

Optional dependencies:

* `scikit-learn` is required when the anomaly-detection algorithms are enabled;
* `pyodbc` is required only when connecting to the WinCC SQL archive.

Fluidit Heat, TIA Portal, and AnyLogic are not required to view the Python source code, documentation, or sample data. The specialized software packages are required only to execute the corresponding models and fully reproduce the experiment.
