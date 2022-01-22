from cgitb import text
import queue
from random import seed
import serial
import serial.tools.list_ports
from signal import signal, SIGINT
import sys
import threading
import time
import tkinter
from tkinter import END, W, PhotoImage, filedialog as fd, scrolledtext as sd

global fw_filename
fw_filename = ""

COM_OVERRIDE=None
VERSION='1.0'

DEBUG=False
running = True

class PrintLogger():
    def __init__(self, textbox):
        self.textbox = textbox
    def write(self, text):
        self.textbox.insert(tkinter.END, text)
        self.textbox.see(END)

    def flush(self):
        pass

def on_closing():
    global running
    running = False

def sig_handler(signal_received, frame):
    on_closing()

class Tink:
    cmd = {
            'CmdGetVer':    b'\x01', 
            'CmdErase':     b'\x02',
            'CmdWrite':     b'\x03',
            'JumpApp':      b'\x05',
            }
    ctrl = {
            'SOH':          b'\x01',
            'EOT':          b'\x04',
            'DLE':          b'\x10',
            }
    
    rxfsm = {
            'RxIdle':       0,
            'RxBuffer':     1,
            'RxEscape':     2,
            }

    blfsm = {
            'BlIdle':       0,
            'BlVersion':    1,
            'BlErase':      2,
            'BlWrite':      3,
            'BlJump':       4,
            }

    serial = None
    rx_state = rxfsm['RxIdle']

    def timer(self, timestamp):
        # 100ms interval timer
        if running:
            timestamp += 0.1
            self.timer_thread = threading.Timer(timestamp - time.time(), self.timer, args=(timestamp,)).start()

    def calc_crc(self, b):
        # NOTE: This is the CRC lookup table for polynomial 0x1021
        lut = [
            0, 4129, 8258, 12387,\
            16516, 20645, 24774, 28903,\
            33032, 37161, 41290, 45419,\
            49548, 53677, 57806, 61935]

        num1 = 0
        for num2 in b:
            num3 = (num1 >> 12) ^ (num2 >> 4)
            num4 = (lut[num3 & 0x0F] ^ (num1 << 4)) & 0xFFFF
            num5 = (num4 >> 12) ^ num2
            num1 = (lut[num5 & 0x0F] ^ (num4 << 4)) & 0xFFFF
        return num1

    def rx_process(self, packet, debug=DEBUG):
        if debug:
            print('Processing packet: %s' % packet.hex())

        crc_rx = (packet[-1] << 8) | packet[-2]
        if self.calc_crc(packet[0:-2]) != crc_rx:
            print('Bad CRC received, resetting state')
            self.bl_state = self.blfsm['BlIdle']

        else:
            cmd = bytes([packet[0]])
            payload = packet[1:-2]
            if self.bl_state == self.blfsm['BlVersion']:
                if cmd == self.cmd['CmdGetVer']:
                    print('Found device ID: %s' % payload.decode().split('\x00')[0])

                    print('Erasing device... ', end='')
                    self.tx_packet(self.cmd['CmdErase'])
                    self.bl_state = self.blfsm['BlErase']
                else:
                    print('ERROR: Expected response code CmdGetVer, got %s' % packet[0])

            elif self.bl_state == self.blfsm['BlErase']:
                if cmd == self.cmd['CmdErase']:
                    print('OKAY')

                    self.hex_line = 1
                    self.fw_file = open(self.fw_name, 'r')
                    tx = bytearray(self.cmd['CmdWrite'])
                    hex_line = bytes.fromhex(self.fw_file.readline().rstrip()[1:])
                    tx += hex_line
                    print('Writing firmware %d/%d... ' % (self.hex_line, self.hex_nline), end='')
                    self.tx_packet(tx)
                    self.bl_state = self.blfsm['BlWrite']
                else:
                    print('ERROR: Expected response code CmdErase, got %s' % packet[0])

            elif self.bl_state == self.blfsm['BlWrite']:
                if cmd == self.cmd['CmdWrite']:
                    print('OKAY')
                    self.hex_line = self.hex_line + 1

                    # hex_line starts at 1, so we need to send up to and
                    # including hex_nline
                    if self.hex_line > self.hex_nline:
                        print('Update complete, booting firmware')
                        self.bl_state = self.blfsm['BlJump']
                        self.tx_packet(self.cmd['JumpApp'])
                        button_state()
                        return
                        # There doesnt seem to be a response to the JumpApp
                        # command, so at this point we're done.
                        self.running = False

                    else:
                        tx = bytearray(self.cmd['CmdWrite'])
                        hex_line = bytes.fromhex(self.fw_file.readline().rstrip()[1:])
                        tx += hex_line
                        print('Writing firmware %d/%d... ' % (self.hex_line, self.hex_nline), end='')
                        self.tx_packet(tx)

                else:
                    print('ERROR: Expected response code CmdWrite, got %s' % packet[0])

    def rx_buffer(self, b, debug=DEBUG):
        state_begin = self.rx_state

        if self.rx_state == self.rxfsm['RxIdle']:
            # Ignore bytes until we see SOH
            if b == self.ctrl['SOH']:
                self.rxbuf = bytearray()
                self.rx_state = self.rxfsm['RxBuffer']

        elif self.rx_state == self.rxfsm['RxBuffer']:
            if b == self.ctrl['DLE']:
                # Escape the next control sequence
                self.rx_state = self.rxfsm['RxEscape']

            elif b == self.ctrl['EOT']:
                # End of transmission
                self.rx_state = self.rxfsm['RxIdle']
                self.rx_process(self.rxbuf)

            else:
                # Buffer the byte
                self.rxbuf += b

        elif self.rx_state == self.rxfsm['RxEscape']:
            # Unconditionally buffer any byte following the escape sequence
            self.rxbuf += b
            self.rx_state = self.rxfsm['RxBuffer']

        else:
            # Shouldn't get here
            print('Unknown state')
            self.rx_state = self.rxfsm['RxIdle']

        if debug:
            keys = list(self.rxfsm.keys())
            vals = list(self.rxfsm.values())
            s0 = vals.index(state_begin)
            s1 = vals.index(self.rx_state)
            print('RX: %s, RX FSM state: %s -> %s' % (b.hex(), keys[s0], keys[s1]))


    def rx(self):
        while running:
            if self.serial:
                b = self.serial.read(1)
                if b:
                    self.rx_buffer(b)
                else:
                    print('RX timeout?')
            else:
                print('Lost serial port')
                time.sleep(1)

    def tx(self, b, debug=DEBUG):
        if debug:
            print('TX: %s' % b.hex())
        if self.serial and self.serial.is_open:
            try:
                self.serial.write(b)
                self.serial.flush()
            except:
                print('TX failure')
                button_state()
                return
        else:
            print('TX failure, serial port not writeable')
            button_state()
            return

    def tx_packet(self, b):
        # b should be a bytearray
        crc = self.calc_crc(b)
        b += bytes([crc & 0xFF])
        b += bytes([(crc >> 8) & 0xFF])
        b_tx = bytearray(self.ctrl['SOH'])
        for bb in b:
            bb = bytes([bb])
            # Escape any control characters that appear in the TX buffer
            if bb == self.ctrl['SOH'] or bb == self.ctrl['EOT'] or bb == self.ctrl['DLE']:
                b_tx += self.ctrl['DLE']
            b_tx += bb
        b_tx += self.ctrl['EOT']
        self.tx(b_tx)

    def __init__(self, fw_name=None, port=None):
        self.rx_state = self.rxfsm['RxIdle']
        self.bl_state = self.blfsm['BlIdle']

        self.fw_name = fw_name
        self.hex_nline = 0
        self.hex_line = 0

        # Ensure the file exists, has valid Intel Hex checksums, and count lines
        try:
            with open(self.fw_name) as fw_file:
                for line in fw_file:
                    self.hex_nline = self.hex_nline + 1
                    line = line.rstrip()[1:]
                    try:
                        checksum = bytes.fromhex(line[-2:])
                    except:
                        print('%s is not a valid hex file' % fw_name)
                        button_state()
                        return
                    # It seems to just load hex if it's blank
                    data = bytes.fromhex(line[:-2])
                    s = bytes([((~(sum(data) & 0xFF) & 0xFF) + 1) & 0xFF])
                    
                    if checksum != s:
                        print('%s is not a valid hex file' % fw_name)
                        button_state()
                        return
        except:
            print('No file selected')
            button_state()
            return

        comports = []
        try:
            if port == None:
                comports_all = [comport for comport in serial.tools.list_ports.comports()]
                for com in comports_all:
                    if com.manufacturer == 'FTDI':
                        comports.append(com.device)
            else:
                comports.append(port)

            if comports:
                if len(comports) > 1:
                    print('Several FTDI devices detected - not sure which to target. Aborting.')
                    # TODO: Add interactive device selector?
                    button_state()
                    return

                for com in comports:
                    try:
                        self.serial = serial.Serial(com, baudrate=115200, timeout=None, rtscts=True)
                        print('Opened device at %s' % com)
                    except Exception as ex:
                        print('Could not open device at %s' % com)
                        print('Exception: %s' % ex)
                        button_state()
                        return
            else:
                print('No RetroTINK devices found')
                button_state()
                return
        except:
            print('No communication with device')
            button_state()
            return


        if self.serial:
            self.rx_process_thread = threading.Thread(target=self.rx, args=())
            self.rx_process_thread.daemon = True
            self.rx_process_thread.start()

            self.timer_thread = threading.Thread(target=self.timer, args=(time.time() + 0.1,))
            self.timer_thread.daemon = True
            self.timer_thread.start()
        else:
            button_state()
            return

        self.running = True

        retries = 1
        self.bl_state = self.blfsm['BlVersion']
        while retries and running:
            retries = retries - 1
            print('Probing device... ', end='')
            self.tx_packet(self.cmd['CmdGetVer'])
            time.sleep(1)
            # Need to add a timeout

def file_select():
    filetypes = (
        ('hex files', '*.hex'),
        ('All files', '*.*')
    )

    fw_filename = fd.askopenfilename(
        title='Select hex',
        initialdir='/',
        filetypes=filetypes)

    browse_box.configure(state="normal")
    browse_box.delete(0, END)
    browse_box.insert(0,fw_filename)
    browse_box.configure(state="readonly")

def tink_flash():
    fw_filename = browse_box.get()
    try:
        button_state()
        tink = Tink(fw_name=fw_filename, port=COM_OVERRIDE)
    except:
        print('Could not execute flash')
        button_state()
        return

def button_state():
    if browse_button['state'] == "normal":
        browse_button.configure(state="disabled")
        flash_button.configure(state="disabled")
    else:
        browse_button.configure(state="normal")
        flash_button.configure(state="normal")

if __name__ == '__main__':
    signal(SIGINT, sig_handler)

    window = tkinter.Tk()
    window.geometry('680x380')
    window.iconbitmap(default='./assets/icon.ico')
    window.title('tinkup-gui')
    window.resizable(False,False)
    window.eval('tk::PlaceWindow . center')
    
    tink_logo = PhotoImage(file='./assets/RetroTINK-logo.png')
    tink_logo = tink_logo.subsample(4,4)
    tink_label = tkinter.Label(window,image=tink_logo)
    tink_label.place(x=285, y=10)

    fw_label = tkinter.Label(window,text="Hex File:")
    fw_label.place(x=325, y=90)

    browse_box = tkinter.Entry(window,textvariable=fw_filename)
    browse_box.configure(state="readonly")
    browse_box.place(x=10, y=120, width=582)
    browse_button = tkinter.Button(window,text='Load HEX',command=file_select)
    browse_button.place(x=610, y=115)
    
    flash_button = tkinter.Button(window, text="Flash", command=tink_flash)
    flash_button.place(x=330, y=145)

    print_text = sd.ScrolledText(window, undo=True)
    print_text.place(x=10, y=180, height=180)
    logger = PrintLogger(print_text)
    sys.stdout = logger

    

    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    finally:
        window.mainloop()

    on_closing()