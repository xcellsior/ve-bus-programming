import serial
import time
from typing import Optional

class VoltageSettings:
    def __init__(self, port='/dev/ttyUSB0', baudrate=2400):
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5
        )
        
    def _calculate_checksum(self, data: bytes) -> int:
        """Calculate checksum for frame."""
        return (256 - sum(data) % 256) % 256

    def _send_command(self, data: bytes) -> Optional[bytes]:
        """Send command and read response."""
        print(f"Sending: {data.hex()}")
        self.ser.write(data)
        time.sleep(0.1)
        if self.ser.in_waiting:
            response = self.ser.read(self.ser.in_waiting)
            print(f"Response: {response.hex()}")
            return response
        return None

    def set_voltage(self, voltage: float, setting_id: int) -> bool:
        """
        Set voltage using the confirmed working command sequence.
        voltage: target voltage (e.g., 56.0 for 56.0V)
        setting_id: 2 for absorption, 3 for float
        """
        # First set address
        addr_cmd = bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB])
        self._send_command(addr_cmd)
        time.sleep(0.1)
        
        # Convert voltage to internal format (multiply by 100 for 0.01V scale)
        value = int(voltage * 100)
        value_bytes = value.to_bytes(2, byteorder='little')
        
        # Construct the write command
        write_cmd = bytes([
            0x07,           # Length
            0xFF,           # Protocol marker
            0x58,           # 'X' command (ascii)
            0x37,           # CommandWriteViaID
            0x01,           # Flags (Both EEPROM and RAM to last betweeen power cycles)
            setting_id,     # Setting ID (2=absorption, 3=float)
        ]) + value_bytes
        
        # Add checksum
        write_cmd += bytes([self._calculate_checksum(write_cmd)])
        
        # Send command
        response = self._send_command(write_cmd)
        return response is not None

    def close(self):
        self.ser.close()

if __name__ == "__main__":
    try:
        setter = VoltageSettings()
        # Example: Set absorption voltage
        if setter.set_voltage(55.8, 2):
            print("\nSuccessfully set absorption voltage to 56.0V")
        else:
            print("\nFailed to set absorption voltage")
            
        time.sleep(1)
        
        # Example: Set float voltage to 54.0V
        if setter.set_voltage(53.8, 3):
            print("\nSuccessfully set float voltage to 54.0V")
        else:
            print("\nFailed to set float voltage")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        setter.close()
