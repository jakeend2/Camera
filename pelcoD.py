import binascii
import socket, datetime, threading, os, serial



class pelcoD():
    def checksum(command):
        checksum = ((command[1]+command[2]+ command[3] +command[4]+ command[5]) % 256)
        command.append(checksum)
        return command
    def panleft(self, speed):
        if speed < 63:
            command = bytearray(b'\xFF\x01\x00\x04\x3F\x00')
            command[4] = speed
        else:
            command = bytearray(b'\xFF\x01\x00\x04\x3F\x00')
            command[4] = 63
        command = pelcoD.checksum(command)
        return command
    def stop(self):
        command = bytearray(b'\xFF\x01\x00\x00\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def panright(self, speed):
        if speed < 63:
            command = bytearray(b'\xFF\x01\x00\x02\x3F\x00')
            command[4] = speed
        else:
            command = bytearray(b'\xFF\x01\x00\x02\x3F\x00')
            command[4] = 63
        command = pelcoD.checksum(command)
        return command
    def tiltup(self, speed):
        if speed < 63:
            command = bytearray(b'\xFF\x01\x00\x08\x00\x3F')
            command[5] = speed
        else:
            command = bytearray(b'\xFF\x01\x00\x08\x00\x3F')
            command[5] = 63
        command = pelcoD.checksum(command)
        return command
    def tiltdown(self, speed):
        if speed < 63:
            command = bytearray(b'\xFF\x01\x00\x10\x00\x3F')
            command[5] = speed
        else:
            command = bytearray(b'\xFF\x01\x00\x10\x00\x3F')
            command[5] = 63
        command = pelcoD.checksum(command)
        return command
    def zoomtele(self):
        command = bytearray(b'\xFF\x01\x00\x20\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def zoomwide(self):
        command = bytearray(b'\xFF\x01\x00\x40\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def focusfar(self):
        command = bytearray(b'\xFF\x01\x00\x80\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def focusnear(self):
        command = bytearray(b'\xFF\x01\x01\x00\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def setpreset(self, preset):
        command = bytearray(b'\xFF\x01\x00\x03\x00\x00')
        command[5] = preset
        command = pelcoD.checksum(command)
        return command
    def gotopreset(self, preset):
        command = bytearray(b'\xFF\x01\x00\x07\x00\x00')
        command[5] = preset
        command = pelcoD.checksum(command)
        return command
    def clearpreset(self, preset):
        command = bytearray(b'\xFF\x01\x00\x05\x00\x00')
        command[5] = preset
        command = pelcoD.checksum(command)
        return command
    def auxon(self, aux):
        command = bytearray(b'\xFF\x01\x00\x09\x00\x00')
        command[5] = aux
        command = pelcoD.checksum(command)
        return command
    def auxoff(self, aux):
        command = bytearray(b'\xFF\x01\x00\x0B\x00\x00')
        command[5] = aux
        command = pelcoD.checksum(command)
        return command
    def setpanposition(self, MSB, LSB):
        command = bytearray(b'\xFF\x01\x00\x00\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def settiltposition(self, MSB, LSB):
        command = bytearray(b'\xFF\x01\x00\x00\x00\x00')
        command = pelcoD.checksum(command)
        return command
    def setzoomposition(self, MSB, LSB):
        command = bytearray(b'\xFF\x01\x00\x00\x00\x00')
        command = pelcoD.checksum(command)
        return command
    
    

    
        






    


