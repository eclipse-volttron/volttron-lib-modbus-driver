# VOLTTRON Modbus Driver Interface

![Python 3.10](https://img.shields.io/badge/python-3.10-blue.svg)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)
[![Passing?](https://github.com/VOLTTRON/volttron-lib-modbus-driver/actions/workflows/run-tests.yml/badge.svg)](https://github.com/VOLTTRON/volttron-lib-modbus-driver/actions/workflows/run-tests.yml)
[![pypi version](https://img.shields.io/pypi/v/volttron-lib-modbus-driver.svg)](https://pypi.org/project/volttron-lib-modbus-driver/)

This interface lets the VOLTTRON Platform Driver poll and command Modbus devices over TCP, UDP, TLS, or serial.
All Modbus communication happens in the [Modbus Protocol Proxy](https://github.com/eclipse-volttron/lib-protocol-proxy-modbus),
a separate process shared by every Modbus device on the platform. The interface declares each device's registers and
their data types to the proxy once, then reads and writes decoded values by address. Grouping registers into requests,
padding, and value encoding are handled by the proxy.

## Pre-requisite

VOLTTRON (>=11.0.0rc0) should be installed and running.  Its virtual environment should be active.
Information on how to install of the VOLTTRON platform can be found
[here](https://github.com/eclipse-volttron/volttron-core/tree/v10)

## Automatically installed dependencies

* volttron-lib-base-driver >= 2.0.0rc5
* protocol-proxy-modbus >= 2.0.0rc3 (which brings pymodbus)

# Documentation
More detailed documentation can be found on [ReadTheDocs](https://eclipse-volttron.readthedocs.io/en/latest/external-docs/volttron-lib-modbus-driver/index.html#modbus-driver). The RST source
of the documentation for this component is located in the "docs" directory of this repository.


# Installation


1. If it is not already, install the VOLTTRON Platform Driver Agent:

    ```shell
    vctl install volttron-platform-driver --vip-identity platform.driver
    ```

1. Install the VOLTTRON Modbus Driver Library:

    ```shell
    poetry add --directory $VOLTTRON_HOME volttron-lib-modbus-driver
    ```

1. Store device and registry files for the Modbus device to the Platform Driver configuration store:

    * Create a config directory and navigate to it:

        ```shell
        mkdir config
        cd config
        ```

    * Create a file called `device_name.config`; it should contain a JSON object that specifies the configuration of your
      Modbus driver. An example of such a file is provided at the root of this project; the example file is named
      'modbus_example.config'. The following JSON is an example of a `modbus.config`:

         ```json
         {
             "driver_type": "modbus",
             "remote_config": {
                 "device_address": "10.0.0.4",
                 "port": 502,
                 "unit_id": 1
             },
             "registry_config": "config://modbus.csv",
             "interval": 15,
             "timezone": "US/Pacific"
         }
         ```

      The legacy key `driver_config` is accepted in place of `remote_config`.

    * Create another file called `device_name.csv`; it should contain all the points on the device that you want
      published to VOLTTRON. An example of such a CSV file is provided at the root of this project; the example CSV file
      is named 'modbus_example_registry.csv'. The following CSV file is an example:

        ```csv
        Volttron Point Name,Units,Writable,Point Address,Modbus Register,Table,Default Value,Notes
        ReturnAirCO2,PPM,FALSE,1001,>f,input,,CO2 reading
        CO2Stpt,PPM,TRUE,1003,>f,holding,1000,Setpoint
        UnitName,,TRUE,1005,string[6],holding,,Unit name
        reserved,,FALSE,1008,pad[2],holding,,Read through but never published
        Mode,,TRUE,1010,uint16,holding,,Operating mode
        FanStatus,,FALSE,3,bool,discrete_input,,Fan proof
        FanCmd,,TRUE,5,bool,coil,,Fan command
        ```

    * Add the Modbus driver config and CSV file to the Platform Driver configuration store:

         ```
         vctl config store platform.driver modbus.csv modbus.csv --csv
         vctl config store platform.driver devices/campus/building/modbus modbus.config
         ```

1. Observe Data

    To see data being published to the bus, install a [Listener Agent](https://github.com/eclipse-volttron/volttron-listener):

    ```
    vctl install volttron-listener --start
    ```

    Once installed, you should see the data being published by viewing the Volttron logs file that was created in step 2.
    To watch the logs, open a separate terminal and run the following command:

    ```
    tail -f <path to folder containing volttron.log>/volttron.log
    ```

# Configuration reference

## Device configuration (`remote_config`)

| Key | Default | Description |
|---|---|---|
| `device_address` | required | Host name or IP of the device or gateway, or the serial device path (`/dev/ttyUSB0`). |
| `transport_protocol` | `tcp` | `tcp`, `udp`, `tls`, or `serial`. |
| `port` | 502 (802 for TLS) | TCP/UDP/TLS port. Ignored for serial. |
| `unit_id` | 1 | Modbus unit (slave) id. `device_id` and `slave_id` are accepted aliases. Each unit gets its own device configuration; units behind one gateway share the proxy's connection to it. |
| `addressing` | `offset` | How `Point Address` is expressed: `offset` (zero-based protocol address; `exact` is a synonym), `offset_plus` (one-based), or `address` (table-prefixed, e.g. 40001 for the first holding register). |
| `word_order` | `big` | Register order for multi-register values. `endian` is an accepted alias. A point may override this. |
| `max_gap` | 0 | Largest run of unconfigured registers the proxy may read through to merge two requests into one. |
| `timeout` | 3 | Seconds the proxy waits for the device to answer one request before retrying. |
| `retries` | 3 | Retries after the first attempt of each request. |
| `reply_timeout` | derived | Seconds the driver waits for the proxy's reply. Defaults to three full timeout-and-retry cycles and at least 30 seconds, so it outlasts requests queued behind other units on a shared gateway. |
| `registration_timeout` | 30 | Seconds to wait for the proxy process to start. |
| `baudrate`, `bytesize`, `parity`, `stopbits` | 9600, 8, `none`, 1 | Serial settings. `parity` is `none`, `even`, `odd`, `mark`, or `space`. |
| `proxy_group` | none | All Modbus devices share one proxy process. Name a group here to give a set of devices their own process. |
| `register_map` | none | Optional modbus_tk-style register map, normally `config://<name>.csv`. Registry rows that lack an address or data type are completed from the map row with the same `Register Name`; see [modbus_tk configurations](#modbus_tk-configurations). Never needed when the registry is complete on its own. |

## Registry columns

| Column | Description |
|---|---|
| `Volttron Point Name` | Topic segment for the point. Required unless `Register Name` is given, which is then used as the point name. |
| `Register Name` | Name shared with a `register_map` row (see below). Also the point name when `Volttron Point Name` is absent. |
| `Point Address` | Register (or coil) address, interpreted according to `addressing`. `Address` is accepted. Decimal, or hexadecimal with a `0x` prefix. |
| `Modbus Register` | Data type. Also accepted under `Data Type`, `Data Format`, or `Type`. Spellings: pymodbus names (`UINT16`, `INT32`, `FLOAT32`, `FLOAT64`, `STRING`, `BITS`), modbus_tk names (`uint16`, `int32`, `float`, `double`, `string[8]`, `bool`), or struct formats (`>f`, `>H`, `4H`, `8s`). |
| `Table` | `coil`, `discrete_input`, `holding`, or `input` (modbus_tk names such as `analog_output_holding_registers` are accepted). Optional: booleans default to `coil` when writable and `discrete_input` otherwise; everything else defaults to `holding` when writable and `input` otherwise. |
| `Writable` | `TRUE` or `FALSE`. Points in read-only tables cannot be writable. |
| `Count` | Number of registers (or coils), when the type does not imply it, e.g. a string without a length. |
| `Word Order` / `Mixed Endian` | Per-point word order. `Mixed Endian: TRUE` is the legacy spelling of `little`. |
| `Default Value` | Value written when the point is reverted. Otherwise the last polled value is used. |
| `Units`, `Notes` | Metadata published with the point. |

### modbus_tk configurations

The modbus_tk driver split a device between a registry (`Volttron Point Name`, `Register Name`) and a register map
named by `register_map` in the device configuration (`Register Name`, `Address`, `Type`, `Units`, `Writable`, and
optionally `Default Value`, `Table`, `Mixed Endian`, `Description`). Such configurations work unchanged. Rows that
already carry an address and a data type are used as they are, so the map is only consulted for rows that need it.
Where a registry row and its map row both supply a field, the registry row wins; blank registry cells do not override
the map. A row that lacks an address or type and has no matching `Register Name` in the map is dropped with a warning
and the rest of the device is configured. A map file may also be used directly as the registry: each row is then
published under its `Register Name`.

### Pad registers

A row whose data type is `pad` (or `pad[n]`) declares registers to read but never publish. Use pads to bridge gaps
between points so that they are fetched in one request. Pad rows must name their `Table`. They are not points: the
platform will not poll or publish them, and they do not appear in the device's topics.

## Requests per poll

When the device registers with the proxy, the driver logs, per table, how many points and pads were configured and
how many requests each poll will take. Adjust pads and `max_gap` to reduce that number.

# Testing

```shell
pytest tests
```

The unit tests run against a fake proxy manager. `tests/test_end_to_end.py` additionally launches a real Modbus
proxy process and a pymodbus TCP simulator on a free local port; it needs no hardware.

`tests/test_driver_local.py` goes one level further: it starts a real VOLTTRON platform in a temporary home, installs
the Platform Driver, configures this driver against a local modbus_tk server, and reads and writes every struct data
type in both byte orders through `vdrv`. It takes a few minutes, mostly platform start-up. Set
`MODBUS_SKIP_PLATFORM_TESTS=1` to skip it. `tests/test_driver_demo_board.py` runs the same flow against real hardware
when `MODBUS_TEST_IP` is set.

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
