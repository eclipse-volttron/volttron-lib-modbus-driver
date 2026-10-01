"""Modbus TCP simulator with known values. Usage: modbus_sim.py PORT"""
import asyncio, sys
from pymodbus.client.mixin import ModbusClientMixin as M
from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncTcpServer

D = M.DATATYPE
hr, ir = [0] * 1300, [0] * 1300          # addresses 0-1299 exist; anything above is an illegal address
co, di = [False] * 100, [False] * 100

def put(block, addr, regs): block[addr:addr + len(regs)] = regs
put(ir, 1001, M.convert_to_registers(412.5, D.FLOAT32))    # ReturnAirCO2 (input)
put(ir, 1010, M.convert_to_registers(-7, D.INT16))          # Temp (input)
put(hr, 1003, M.convert_to_registers(1000.0, D.FLOAT32))   # CO2Stpt (holding)
put(hr, 1005, M.convert_to_registers('RTU-7', D.STRING) + [0])  # Name (holding, 3 regs)
put(hr, 1010, [3])                                          # Mode (holding)
di[3] = True                                                # FanStatus
co[5] = False                                               # FanCmd

ctx = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, ir),
                          co=ModbusSequentialDataBlock(1, co), di=ModbusSequentialDataBlock(1, di))
asyncio.run(StartAsyncTcpServer(ModbusServerContext(devices=ctx, single=True), address=('127.0.0.1', int(sys.argv[1]))))
