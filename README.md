# volttron-lib-modbus-driver

[![Eclipse VOLTTRON™](https://img.shields.io/badge/Eclips%20VOLTTRON--red.svg)](https://eclipse-volttron.readthedocs.io/en/latest/)
![Python 3.10](https://img.shields.io/badge/python-3.10-blue.svg)
![Passing?](https://github.com/VOLTTRON/volttron-lib-modbus-driver/actions/workflows/run-tests.yml/badge.svg)
[![pypi version](https://img.shields.io/pypi/v/volttron-lib-modbus-driver.svg)](https://pypi.org/project/volttron-lib-modbus-driver/)

> **Note**
> VOLTTRON’s modbus driver supports the Modbus over TCP/IP protocol only.


## Requires

* python >=3.10
* volttron-lib-base-driver >= 2.0.0rc0
* pymodbus >= 3.7.2


# Documentation
More detailed documentation can be found on [ReadTheDocs](https://eclipse-volttron.readthedocs.io/en/latest/external-docs/volttron-lib-modbus-driver_docs_root/docs/source/index.html#modbus-driver). The RST source
of the documentation for this component is located in the "docs" directory of this repository.


# Installation

Before installing, VOLTTRON should be installed and running.  Its virtual environment should be active.
Information on how to install of the VOLTTRON platform can be found
[here](https://github.com/eclipse-volttron/volttron-core).

1. If it is not already, install the VOLTTRON Platform Driver Agent:

    ```shell
    vctl install volttron-platform-driver --vip-identity platform.driver
    ```

2. Install the volttron-lib-modbus-driver library.

    ```shell
    poetry add --directory $VOLTTRON_HOME volttron-lib-modbus-driver
    ```

3. Store device and registry files for the Modbus device to the Platform Driver configuration store:

    Create a driver configuration file called `modbus.config`. There are three arguments for the driver_config section of the device configuration file:

    * device_address:  IP Address of the device.

    * port: Port the device is listening on. Defaults to 502 which is the standard port for Modbus devices.

    * slave_id:  Slave ID of the device. Defaults to 0. Use 0 for no slave.

    This repo provides an example configuration in the file "modbus_example.config".

    Here is an example device configuration file:

    ```json
    {
        "driver_config": {"device_address": "10.0.0.4"},
        "driver_type": "modbus",
        "registry_config":"config://modbus.csv",
        "interval": 120,
        "timezone": "UTC",
        "campus": "campus",
        "building": "building",
        "unit": "modbus1",
        "heart_beat_point": "ESMMode"
    }
    ```

    Create another file called `mobus.csv`. This CSV file will be your modbus registry configuration file. Each row configures a point on the device.

    The following columns are required for each row:

    * Volttron Point Name - The name by which the platform and agents will refer to this point.

    * Units - Used for meta data when creating point information on the historian.

    * Modbus Register - A string representing how to interpret the binary format of the data register. The string takes two forms:

        * “BOOL” for coils and discrete inputs.

        * A format string for the Python struct module. See the [Python3 Struct docs](http://docs.python.org/3/library/struct.html) for full documentation. The supplied format string must only represent one value. See the documentation of your device to determine how to interpret the registers. Some Examples:

        * “>f” - A big endian 32-bit floating point number.

        * “<H” - A little endian 16-bit unsigned integer.

        * “>l” - A big endian 32-bit integer.

    * Writable - Either TRUE or FALSE. Determines if the point can be written to.

    * Point Address - Modbus address of the point. Cannot include any offset value, it must be the exact value of the address.

    * Mixed Endian - (Optional) Either TRUE or FALSE. For mixed endian values. This will reverse the order of the Modbus registers that make up this point before parsing the value or writing it out to the device. Has no effect on bit values.

    The following column is optional:

    * Default Value - The default value for the point. When the point is reverted by an agent it will change back to this value. If this value is missing it will revert to the last known value not set by an agent.

    Any additional columns will be ignored. It is common practice to include a Point Name or Reference Point Name to include the device documentation’s name for the point and Notes and Unit Details for additional information about a point.

    The following is an example of a Modbus registry configuration file:

    ```csv
    Reference Point Name,Volttron Point Name,Units,Units Details,Modbus Register,Writable,Point Address,Default Value,Notes
    CO2Sensor,ReturnAirCO2,PPM,0.00-2000.00,>f,FALSE,1001,,CO2 Reading 0.00-2000.0 ppm
    CO2Stpt,ReturnAirCO2Stpt,PPM,1000.00 (default),>f,TRUE,1011,1000,Setpoint to enable demand control ventilation
    Cool1Spd,CoolSupplyFanSpeed1,%,0.00 to 100.00 (75 default),>f,TRUE,1005,75,Fan speed on cool 1 call
    Cool2Spd,CoolSupplyFanSpeed2,%,0.00 to 100.00 (90 default),>f,TRUE,1007,90,Fan speed on Cool2 Call
    Damper,DamperSignal,%,0.00 - 100.00,>f,FALSE,1023,,Output to the economizer damper
    DaTemp,DischargeAirTemperature,F,(-)39.99 to 248.00,>f,FALSE,1009,,Discharge air reading
    ESMEconMin,ESMDamperMinPosition,%,0.00 to 100.00 (5 default),>f,TRUE,1013,5,Minimum damper position during the energy savings mode
    FanPower,SupplyFanPower, kW,0.00 to 100.00,>f,FALSE,1015,,Fan power from drive
    FanSpeed,SupplyFanSpeed,%,0.00 to 100.00,>f,FALSE,1003,,Fan speed from drive
    HeatCall1,HeatCall1,On / Off,on/off,BOOL,FALSE,1113,,Status indicator of heating stage 1 need
    HeartBeat,heartbeat,On / Off,on/off,BOOL,FALSE,1114,,Status indicator of heating stage 2 need
    ```

    Add modbus.csv and modbus.config to the configuration store:

    ```
    vctl config store platform.driver devices/campus/building/modbus modbus.config
    vctl config store platform.driver modbus.csv modbus.csv --csv
    ```

4. Observe Data

    To see data being published to the bus, install a [Listener Agent](https://pypi.org/project/volttron-listener/):

    ```
    vctl install volttron-listener --start
    ```

    Once installed, you should see the data being published by viewing the Volttron logs file that was created in step 2.
    To watch the logs, open a separate terminal and run the following command:

    ```
    tail -f <path to folder containing volttron.log>/volttron.log
    ```

# Development

Please see the following for contributing guidelines [contributing](https://github.com/eclipse-volttron/volttron-core/blob/develop/CONTRIBUTING.md).

Please see the following helpful guide about [developing modular VOLTTRON agents](https://eclipse-volttron.readthedocs.io/en/latest/developing-volttron/developing-agents/agent-development.html)


# Disclaimer Notice

This material was prepared as an account of work sponsored by an agency of the
United States Government.  Neither the United States Government nor the United
States Department of Energy, nor Battelle, nor any of their employees, nor any
jurisdiction or organization that has cooperated in the development of these
materials, makes any warranty, express or implied, or assumes any legal
liability or responsibility for the accuracy, completeness, or usefulness or any
information, apparatus, product, software, or process disclosed, or represents
that its use would not infringe privately owned rights.

Reference herein to any specific commercial product, process, or service by
trade name, trademark, manufacturer, or otherwise does not necessarily
constitute or imply its endorsement, recommendation, or favoring by the United
States Government or any agency thereof, or Battelle Memorial Institute. The
views and opinions of authors expressed herein do not necessarily state or
reflect those of the United States Government or any agency thereof.
