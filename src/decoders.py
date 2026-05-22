try:
    import sigrokdecode as srd  # type: ignore
except ImportError:
    class _DummyDecoder:
        pass

    class _DummySrd:
        OUTPUT_PYTHON = 0
        OUTPUT_ANN = 1
        OUTPUT_BINARY = 2
        OUTPUT_META = 3
        SRD_CONF_SAMPLERATE = 0
        Decoder = _DummyDecoder

    srd = _DummySrd()
from collections import namedtuple
from math import floor, ceil
from common.srdhelper import bitpack, bitpack_msb

Data = namedtuple('Data', ['ss', 'es', 'val'])

proto = {
    'START':         [0, 'Start', 'S'],
    'START REPEAT':  [1, 'Start repeat', 'Sr'],
    'STOP':          [2, 'Stop', 'P'],
    'ACK':           [3, 'ACK', 'A'],
    'NACK':          [4, 'NACK', 'N'],
    'BIT':           [5, '{b:1d}'],
    'ADDRESS READ':  [6, 'Address read: {b:02X}', 'AR: {b:02X}', '{b:02X}'],
    'ADDRESS WRITE': [7, 'Address write: {b:02X}', 'AW: {b:02X}', '{b:02X}'],
    'DATA READ':     [8, 'Data read: {b:02X}', 'DR: {b:02X}', '{b:02X}'],
    'DATA WRITE':    [9, 'Data write: {b:02X}', 'DW: {b:02X}', '{b:02X}'],
    'WARN':          [10, '{text}'],
}

spi_mode = {
    (0, 0): 0,
    (0, 1): 1,
    (1, 0): 2,
    (1, 1): 3,
}

RX = 0
TX = 1

def parity_ok(parity_type, parity_bit, data, data_bits):
    if parity_type == 'ignore':
        return True

    if parity_type == 'zero':
        return parity_bit == 0
    elif parity_type == 'one':
        return parity_bit == 1

    ones = bin(data).count('1') + parity_bit

    if parity_type == 'odd':
        return (ones % 2) == 1
    elif parity_type == 'even':
        return (ones % 2) == 0

class Ann:
    RX_DATA, TX_DATA, RX_START, TX_START, RX_PARITY_OK, TX_PARITY_OK, \
    RX_PARITY_ERR, TX_PARITY_ERR, RX_STOP, TX_STOP, RX_WARN, TX_WARN, \
    RX_DATA_BIT, TX_DATA_BIT, RX_BREAK, TX_BREAK, RX_PACKET, TX_PACKET = \
    range(18)

class Bin:
    RX, TX, RXTX = range(3)

class ChannelError(Exception):
    pass

class SamplerateError(Exception):
    pass

class I2CDecoder(srd.Decoder):
    api_version = 3
    id = 'i2c'
    name = 'I²C'
    longname = 'Inter-Integrated Circuit'
    desc = 'Two-wire, multi-master, serial bus.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = ['i2c']
    tags = ['Embedded/industrial']
    channels = (
        {'id': 'scl', 'name': 'SCL', 'desc': 'Serial clock line'},
        {'id': 'sda', 'name': 'SDA', 'desc': 'Serial data line'},
    )
    options = (
        {'id': 'address_format', 'desc': 'Displayed slave address format',
            'default': 'shifted', 'values': ('shifted', 'unshifted')},
    )
    annotations = (
        ('start', 'Start condition'),
        ('repeat-start', 'Repeat start condition'),
        ('stop', 'Stop condition'),
        ('ack', 'ACK'),
        ('nack', 'NACK'),
        ('bit', 'Data/address bit'),
        ('address-read', 'Address read'),
        ('address-write', 'Address write'),
        ('data-read', 'Data read'),
        ('data-write', 'Data write'),
        ('warning', 'Warning'),
    )
    annotation_rows = (
        ('bits', 'Bits', (5,)),
        ('addr-data', 'Address/data', (0, 1, 2, 3, 4, 6, 7, 8, 9)),
        ('warnings', 'Warnings', (10,)),
    )
    binary = (
        ('address-read', 'Address read'),
        ('address-write', 'Address write'),
        ('data-read', 'Data read'),
        ('data-write', 'Data write'),
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.samplerate = None
        self.is_write = None
        self.rem_addr_bytes = None
        self.slave_addr_7 = None
        self.slave_addr_10 = None
        self.is_repeat_start = False
        self.pdu_start = None
        self.pdu_bits = 0
        self.data_bits = []
        self.bitwidth = 0

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def start(self):
        self.out_python = self.register(srd.OUTPUT_PYTHON)
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_binary = self.register(srd.OUTPUT_BINARY)
        self.out_bitrate = self.register(srd.OUTPUT_META,
                meta=(int, 'Bitrate', 'Bitrate from Start bit to Stop bit'))

    def putg(self, ss, es, cls, text):
        self.put(ss, es, self.out_ann, [cls, text])

    def putp(self, ss, es, data):
        self.put(ss, es, self.out_python, data)

    def putb(self, ss, es, data):
        self.put(ss, es, self.out_binary, data)

    def _wants_start(self):
        return self.pdu_start is None

    def _collects_address(self):
        return self.rem_addr_bytes is None or self.rem_addr_bytes != 0

    def _collects_byte(self):
        return self.data_bits is None or len(self.data_bits) < 8

    def handle_start(self, ss, es):
        if self.is_repeat_start:
            cmd = 'START REPEAT'
        else:
            cmd = 'START'
            self.pdu_start = ss
            self.pdu_bits = 0
        self.putp(ss, es, [cmd, None])
        cls, texts = proto[cmd][0], proto[cmd][1:]
        self.putg(ss, es, cls, texts)
        self.is_repeat_start = True
        self.is_write = None
        self.slave_addr_7 = None
        self.slave_addr_10 = None
        self.rem_addr_bytes = None
        self.data_bits.clear()
        self.bitwidth = 0

    def handle_address_or_data(self, ss, es, value):
        self.pdu_bits += 1
        if self.data_bits:
            self.data_bits[-1][2] = ss
        self.data_bits.append([value, ss, es])
        if len(self.data_bits) < 8:
            return
        self.bitwidth = self.data_bits[-2][2] - self.data_bits[-3][2]
        self.data_bits[-1][2] = self.data_bits[-1][1] + self.bitwidth

        d = bitpack_msb(self.data_bits, 0)
        ss_byte, es_byte = self.data_bits[0][1], self.data_bits[-1][2]

        is_address = self._collects_address()
        if is_address:
            addr_byte = d
            if self.rem_addr_bytes is None:
                if (addr_byte & 0xf8) == 0xf0:
                    self.rem_addr_bytes = 2
                    self.slave_addr_7 = None
                    self.slave_addr_10 = addr_byte & 0x06
                    self.slave_addr_10 <<= 7
                else:
                    self.rem_addr_bytes = 1
                    self.slave_addr_7 = addr_byte >> 1
                    self.slave_addr_10 = None
            has_rw_bit = self.is_write is None
            if self.is_write is None:
                read_bit = bool(addr_byte & 1)
                if self.options['address_format'] == 'shifted':
                    d >>= 1
                self.is_write = False if read_bit else True
            elif self.slave_addr_10 is not None:
                self.slave_addr_10 |= addr_byte
            else:
                cls, texts = proto['WARN'][0], proto['WARN'][1:]
                msg = 'Unhandled address byte'
                texts = [t.format(text = msg) for t in texts]
                self.putg(ss_byte, es_byte, cls, texts)
        is_write = self.is_write

        bin_class = -1
        if is_address and is_write:
            cmd = 'ADDRESS WRITE'
            bin_class = 1
        elif is_address and not is_write:
            cmd = 'ADDRESS READ'
            bin_class = 0
        elif not is_address and is_write:
            cmd = 'DATA WRITE'
            bin_class = 3
        elif not is_address and not is_write:
            cmd = 'DATA READ'
            bin_class = 2

        lsb_bits = self.data_bits[:]
        lsb_bits.reverse()
        self.putp(ss_byte, es_byte, ['BITS', lsb_bits])
        self.putp(ss_byte, es_byte, [cmd, d])
        self.putb(ss_byte, es_byte, [bin_class, bytes([d])])

        for bit_value, ss_bit, es_bit in lsb_bits:
            cls, texts = proto['BIT'][0], proto['BIT'][1:]
            texts = [t.format(b = bit_value) for t in texts]
            self.putg(ss_bit, es_bit, cls, texts)

        if is_address and has_rw_bit:
            ss_bit, es_bit = self.data_bits[-1][1], self.data_bits[-1][2]
            es_byte = self.data_bits[-2][2]
            cls = proto[cmd][0]
            w = ['Write', 'Wr', 'W'] if self.is_write else ['Read', 'Rd', 'R']
            self.putg(ss_bit, es_bit, cls, w)

        cls, texts = proto[cmd][0], proto[cmd][1:]
        texts = [t.format(b = d) for t in texts]
        self.putg(ss_byte, es_byte, cls, texts)

    def get_ack(self, ss, es, value):
        ss_bit, es_bit = ss, es
        cmd = 'ACK' if value == 0 else 'NACK'
        self.putp(ss_bit, es_bit, [cmd, None])
        cls, texts = proto[cmd][0], proto[cmd][1:]
        self.putg(ss_bit, es_bit, cls, texts)
        if self.rem_addr_bytes:
            self.rem_addr_bytes -= 1
        self.data_bits.clear()

    def handle_stop(self, ss, es):
        if self.samplerate and self.pdu_start:
            elapsed = es - self.pdu_start + 1
            elapsed /= self.samplerate
            bitrate = int(1 / elapsed * self.pdu_bits)
            ss_meta, es_meta = self.pdu_start, es
            self.put(ss_meta, es_meta, self.out_bitrate, bitrate)
            self.pdu_start = None
            self.pdu_bits = 0

        cmd = 'STOP'
        self.putp(ss, es, [cmd, None])
        cls, texts = proto[cmd][0], proto[cmd][1:]
        self.putg(ss, es, cls, texts)
        self.is_repeat_start = False
        self.is_write = None
        self.data_bits.clear()

    def decode(self):
        while True:
            if self._wants_start():
                pins = self.wait({0: 'h', 1: 'f'})
                ss, es = self.samplenum, self.samplenum
                self.handle_start(ss, es)
            elif self._collects_address() and self._collects_byte():
                pins = self.wait({0: 'r'})
                _, sda = pins
                ss, es = self.samplenum, self.samplenum + self.bitwidth
                self.handle_address_or_data(ss, es, sda)
            elif self._collects_byte():
                pins = self.wait([{0: 'r'}, {0: 'h', 1: 'f'}, {0: 'h', 1: 'r'}])
                if self.matched[0]:
                    _, sda = pins
                    ss, es = self.samplenum, self.samplenum + self.bitwidth
                    self.handle_address_or_data(ss, es, sda)
                elif self.matched[1]:
                    ss, es = self.samplenum, self.samplenum
                    self.handle_start(ss, es)
                elif self.matched[2]:
                    ss, es = self.samplenum, self.samplenum
                    self.handle_stop(ss, es)
            else:
                pins = self.wait({0: 'r'})
                _, sda = pins
                ss, es = self.samplenum, self.samplenum + self.bitwidth
                self.get_ack(ss, es, sda)


class SPIDecoder(srd.Decoder):
    api_version = 3
    id = 'spi'
    name = 'SPI'
    longname = 'Serial Peripheral Interface'
    desc = 'Full-duplex, synchronous, serial bus.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = ['spi']
    tags = ['Embedded/industrial']
    channels = (
        {'id': 'clk', 'name': 'CLK', 'desc': 'Clock'},
    )
    optional_channels = (
        {'id': 'miso', 'name': 'MISO', 'desc': 'Master in, slave out'},
        {'id': 'mosi', 'name': 'MOSI', 'desc': 'Master out, slave in'},
        {'id': 'cs', 'name': 'CS#', 'desc': 'Chip-select'},
    )
    options = (
        {'id': 'cs_polarity', 'desc': 'CS# polarity', 'default': 'active-low',
            'values': ('active-low', 'active-high')},
        {'id': 'cpol', 'desc': 'Clock polarity', 'default': 0, 'values': (0, 1)},
        {'id': 'cpha', 'desc': 'Clock phase', 'default': 0, 'values': (0, 1)},
        {'id': 'bitorder', 'desc': 'Bit order',
            'default': 'msb-first', 'values': ('msb-first', 'lsb-first')},
        {'id': 'wordsize', 'desc': 'Word size', 'default': 8},
    )
    annotations = (
        ('miso-data', 'MISO data'),
        ('mosi-data', 'MOSI data'),
        ('miso-bit', 'MISO bit'),
        ('mosi-bit', 'MOSI bit'),
        ('warning', 'Warning'),
        ('miso-transfer', 'MISO transfer'),
        ('mosi-transfer', 'MOSI transfer'),
    )
    annotation_rows = (
        ('miso-bits', 'MISO bits', (2,)),
        ('miso-data-vals', 'MISO data', (0,)),
        ('miso-transfers', 'MISO transfers', (5,)),
        ('mosi-bits', 'MOSI bits', (3,)),
        ('mosi-data-vals', 'MOSI data', (1,)),
        ('mosi-transfers', 'MOSI transfers', (6,)),
        ('other', 'Other', (4,)),
    )
    binary = (
        ('miso', 'MISO'),
        ('mosi', 'MOSI'),
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.samplerate = None
        self.bitcount = 0
        self.misodata = self.mosidata = 0
        self.misobits = []
        self.mosibits = []
        self.misobytes = []
        self.mosibytes = []
        self.ss_block = -1
        self.ss_transfer = -1
        self.cs_was_deasserted = False
        self.have_cs = self.have_miso = self.have_mosi = None

    def start(self):
        self.out_python = self.register(srd.OUTPUT_PYTHON)
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_binary = self.register(srd.OUTPUT_BINARY)
        self.out_bitrate = self.register(srd.OUTPUT_META,
                meta=(int, 'Bitrate', 'Bitrate during transfers'))
        self.bw = (self.options['wordsize'] + 7) // 8

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def putw(self, data):
        self.put(self.ss_block, self.samplenum, self.out_ann, data)

    def putdata(self):
        so = self.misodata if self.have_miso else None
        si = self.mosidata if self.have_mosi else None
        so_bits = self.misobits if self.have_miso else None
        si_bits = self.mosibits if self.have_mosi else None

        if self.have_miso:
            ss, es = self.misobits[-1][1], self.misobits[0][2]
            bdata = so.to_bytes(self.bw, byteorder='big')
            self.put(ss, es, self.out_binary, [0, bdata])
        if self.have_mosi:
            ss, es = self.mosibits[-1][1], self.mosibits[0][2]
            bdata = si.to_bytes(self.bw, byteorder='big')
            self.put(ss, es, self.out_binary, [1, bdata])

        self.put(ss, es, self.out_python, ['BITS', si_bits, so_bits])
        self.put(ss, es, self.out_python, ['DATA', si, so])

        if self.have_miso:
            self.misobytes.append(Data(ss=ss, es=es, val=so))
        if self.have_mosi:
            self.mosibytes.append(Data(ss=ss, es=es, val=si))

        if self.have_miso:
            for bit in self.misobits:
                self.put(bit[1], bit[2], self.out_ann, [2, ['%d' % bit[0]]])
        if self.have_mosi:
            for bit in self.mosibits:
                self.put(bit[1], bit[2], self.out_ann, [3, ['%d' % bit[0]]])

        if self.have_miso:
            self.put(ss, es, self.out_ann, [0, ['%02X' % self.misodata]])
        if self.have_mosi:
            self.put(ss, es, self.out_ann, [1, ['%02X' % self.mosidata]])

    def reset_decoder_state(self):
        self.misodata = 0 if self.have_miso else None
        self.mosidata = 0 if self.have_mosi else None
        self.misobits = [] if self.have_miso else None
        self.mosibits = [] if self.have_mosi else None
        self.bitcount = 0

    def cs_asserted(self, cs):
        active_low = (self.options['cs_polarity'] == 'active-low')
        return (cs == 0) if active_low else (cs == 1)

    def handle_bit(self, miso, mosi, clk, cs):
        if self.bitcount == 0:
            self.ss_block = self.samplenum
            self.cs_was_deasserted = not self.cs_asserted(cs) if self.have_cs else False

        ws = self.options['wordsize']
        bo = self.options['bitorder']

        if self.have_miso:
            if bo == 'msb-first':
                self.misodata |= miso << (ws - 1 - self.bitcount)
            else:
                self.misodata |= miso << self.bitcount

        if self.have_mosi:
            if bo == 'msb-first':
                self.mosidata |= mosi << (ws - 1 - self.bitcount)
            else:
                self.mosidata |= mosi << self.bitcount

        es = self.samplenum
        if self.bitcount > 0:
            if self.have_miso:
                es += self.samplenum - self.misobits[0][1]
            elif self.have_mosi:
                es += self.samplenum - self.mosibits[0][1]

        if self.have_miso:
            self.misobits.insert(0, [miso, self.samplenum, es])
        if self.have_mosi:
            self.mosibits.insert(0, [mosi, self.samplenum, es])

        if self.bitcount > 0 and self.have_miso:
            self.misobits[1][2] = self.samplenum
        if self.bitcount > 0 and self.have_mosi:
            self.mosibits[1][2] = self.samplenum

        self.bitcount += 1
        if self.bitcount != ws:
            return

        self.putdata()

        if self.samplerate:
            elapsed = 1 / float(self.samplerate)
            elapsed *= (self.samplenum - self.ss_block + 1)
            bitrate = int(1 / elapsed * ws)
            self.put(self.ss_block, self.samplenum, self.out_bitrate, bitrate)

        if self.have_cs and self.cs_was_deasserted:
            self.putw([4, ['CS# was deasserted during this data word!']])

        self.reset_decoder_state()

    def find_clk_edge(self, miso, mosi, clk, cs, first):
        if self.have_cs and (first or self.matched[self.have_cs]):
            oldcs = None if first else 1 - cs
            self.put(self.samplenum, self.samplenum, self.out_python, ['CS-CHANGE', oldcs, cs])

            if self.cs_asserted(cs):
                self.ss_transfer = self.samplenum
                self.misobytes = []
                self.mosibytes = []
            elif self.ss_transfer != -1:
                if self.have_miso:
                    self.put(self.ss_transfer, self.samplenum, self.out_ann,
                        [5, [' '.join(format(x.val, '02X') for x in self.misobytes)]])
                if self.have_mosi:
                    self.put(self.ss_transfer, self.samplenum, self.out_ann,
                        [6, [' '.join(format(x.val, '02X') for x in self.mosibytes)]])
                self.put(self.ss_transfer, self.samplenum, self.out_python, ['TRANSFER', self.mosibytes, self.misobytes])

            self.reset_decoder_state()

        if self.have_cs and not self.cs_asserted(cs):
            return

        if first or not self.matched[0]:
            return

        mode = spi_mode[self.options['cpol'], self.options['cpha']]
        if mode == 0 and clk == 0:
            return
        elif mode == 1 and clk == 1:
            return
        elif mode == 2 and clk == 1:
            return
        elif mode == 3 and clk == 0:
            return

        self.handle_bit(miso, mosi, clk, cs)

    def decode(self):
        if not self.has_channel(0):
            raise ChannelError('Either MISO or MOSI (or both) pins required.')
        self.have_miso = self.has_channel(1)
        self.have_mosi = self.has_channel(2)
        if not self.have_miso and not self.have_mosi:
            raise ChannelError('Either MISO or MOSI (or both) pins required.')
        self.have_cs = self.has_channel(3)
        if not self.have_cs:
            self.put(0, 0, self.out_python, ['CS-CHANGE', None, None])

        wait_cond = [{0: 'e'}]
        if self.have_cs:
            self.have_cs = len(wait_cond)
            wait_cond.append({3: 'e'})

        (clk, miso, mosi, cs) = self.wait({})
        self.find_clk_edge(miso, mosi, clk, cs, True)

        while True:
            (clk, miso, mosi, cs) = self.wait(wait_cond)
            self.find_clk_edge(miso, mosi, clk, cs, False)


class PWMDecoder(srd.Decoder):
    api_version = 3
    id = 'pwm'
    name = 'PWM'
    longname = 'Pulse-width modulation'
    desc = 'Analog level encoded in duty cycle percentage.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = []
    tags = ['Encoding']
    channels = (
        {'id': 'data', 'name': 'Data', 'desc': 'Data line'},
    )
    options = (
        {'id': 'polarity', 'desc': 'Polarity', 'default': 'active-high',
            'values': ('active-low', 'active-high')},
    )
    annotations = (
        ('duty-cycle', 'Duty cycle'),
        ('period', 'Period'),
        ('frequency', 'Frequency'),
    )
    annotation_rows = (
         ('duty-cycle-vals', 'Duty cycles', (0,)),
         ('periods', 'Periods', (1,)),
         ('frequency-vals', 'Frequencies', (2,)),
    )
    binary = (
        ('raw', 'RAW file'),
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.samplerate = None
        self.ss_block = self.es_block = None

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_binary = self.register(srd.OUTPUT_BINARY)
        self.out_average = \
            self.register(srd.OUTPUT_META,
                          meta=(float, 'Average', 'PWM base (cycle) frequency'))

    def putx(self, data):
        self.put(self.ss_block, self.es_block, self.out_ann, data)

    def putp(self, period_t):
        if period_t == 0 or period_t >= 1:
            period_s = '%.1f s' % (period_t)
        elif period_t <= 1e-12:
            period_s = '%.1f fs' % (period_t * 1e15)
        elif period_t <= 1e-9:
            period_s = '%.1f ps' % (period_t * 1e12)
        elif period_t <= 1e-6:
            period_s = '%.1f ns' % (period_t * 1e9)
        elif period_t <= 1e-3:
            period_s = '%.1f μs' % (period_t * 1e6)
        else:
            period_s = '%.1f ms' % (period_t * 1e3)

        self.put(self.ss_block, self.es_block, self.out_ann, [1, [period_s]])

    def putf(self, period_t):
        if period_t != 0:
            frequency = 1 / period_t

            if frequency >= 1e15:
                frequency_s = '%.3f PHz' % (frequency / 1e15)
            elif frequency >= 1e12:
                frequency_s = '%.3f THz' % (frequency / 1e12)
            elif frequency >= 1e9:
                frequency_s = '%.3f GHz' % (frequency / 1e9)
            elif frequency >= 1e6:
                frequency_s = '%.3f MHz' % (frequency / 1e6)
            elif frequency >= 1e3:
                frequency_s = '%.3f kHz' % (frequency / 1e3)
            else:
                frequency_s = '%.3f Hz' % (frequency)

            self.put(self.ss_block, self.es_block, self.out_ann, [2, [frequency_s]])
        else:
            self.put(self.ss_block, self.es_block, self.out_ann, [2, ["invalid"]])

    def putb(self, data):
        self.put(self.ss_block, self.es_block, self.out_binary, data)

    def decode(self):
        if not self.samplerate:
            raise SamplerateError('Cannot decode without samplerate.')

        num_cycles = 0
        average = 0

        self.wait({0: 'f' if self.options['polarity'] == 'active-low' else 'r'})
        self.first_samplenum = self.samplenum

        while True:
            start_samplenum = self.samplenum
            self.wait({0: 'e'})
            end_samplenum = self.samplenum
            self.wait({0: 'e'})
            self.ss_block = start_samplenum
            self.es_block = self.samplenum

            period = self.samplenum - start_samplenum
            duty = end_samplenum - start_samplenum
            ratio = float(duty / period)

            percent = float(ratio * 100)
            self.putx([0, ['%f%%' % percent]])

            self.putb([0, bytes([int(ratio * 256)])])

            period_t = float(period / self.samplerate)
            self.putp(period_t)
            self.putf(period_t)

            num_cycles += 1
            average += percent
            self.put(self.first_samplenum, self.es_block, self.out_average,
                     float(average / num_cycles))


class UARTDecoder(srd.Decoder):
    api_version = 3
    id = 'uart'
    name = 'UART'
    longname = 'Universal Asynchronous Receiver/Transmitter'
    desc = 'Asynchronous, serial bus.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = ['uart']
    tags = ['Embedded/industrial']
    optional_channels = (
        {'id': 'rx', 'name': 'RX', 'desc': 'UART receive line'},
        {'id': 'tx', 'name': 'TX', 'desc': 'UART transmit line'},
    )
    options = (
        {'id': 'baudrate', 'desc': 'Baud rate', 'default': 115200},
        {'id': 'data_bits', 'desc': 'Data bits', 'default': 8,
            'values': (5, 6, 7, 8, 9)},
        {'id': 'parity', 'desc': 'Parity', 'default': 'none',
            'values': ('none', 'odd', 'even', 'zero', 'one', 'ignore')},
        {'id': 'stop_bits', 'desc': 'Stop bits', 'default': 1.0,
            'values': (0.0, 0.5, 1.0, 1.5, 2.0)},
        {'id': 'bit_order', 'desc': 'Bit order', 'default': 'lsb-first',
            'values': ('lsb-first', 'msb-first')},
        {'id': 'format', 'desc': 'Data format', 'default': 'hex',
            'values': ('ascii', 'dec', 'hex', 'oct', 'bin')},
        {'id': 'invert_rx', 'desc': 'Invert RX', 'default': 'no',
            'values': ('yes', 'no')},
        {'id': 'invert_tx', 'desc': 'Invert TX', 'default': 'no',
            'values': ('yes', 'no')},
        {'id': 'sample_point', 'desc': 'Sample point (%)', 'default': 50},
        {'id': 'rx_packet_delim', 'desc': 'RX packet delimiter (decimal)',
            'default': -1},
        {'id': 'tx_packet_delim', 'desc': 'TX packet delimiter (decimal)',
            'default': -1},
        {'id': 'rx_packet_len', 'desc': 'RX packet length', 'default': -1},
        {'id': 'tx_packet_len', 'desc': 'TX packet length', 'default': -1},
    )
    annotations = (
        ('rx-data', 'RX data'),
        ('tx-data', 'TX data'),
        ('rx-start', 'RX start bit'),
        ('tx-start', 'TX start bit'),
        ('rx-parity-ok', 'RX parity OK bit'),
        ('tx-parity-ok', 'TX parity OK bit'),
        ('rx-parity-err', 'RX parity error bit'),
        ('tx-parity-err', 'TX parity error bit'),
        ('rx-stop', 'RX stop bit'),
        ('tx-stop', 'TX stop bit'),
        ('rx-warning', 'RX warning'),
        ('tx-warning', 'TX warning'),
        ('rx-data-bit', 'RX data bit'),
        ('tx-data-bit', 'TX data bit'),
        ('rx-break', 'RX break'),
        ('tx-break', 'TX break'),
        ('rx-packet', 'RX packet'),
        ('tx-packet', 'TX packet'),
    )
    annotation_rows = (
        ('rx-data-bits', 'RX bits', (Ann.RX_DATA_BIT,)),
        ('rx-data-vals', 'RX data', (Ann.RX_DATA, Ann.RX_START, Ann.RX_PARITY_OK, Ann.RX_PARITY_ERR, Ann.RX_STOP)),
        ('rx-warnings', 'RX warnings', (Ann.RX_WARN,)),
        ('rx-breaks', 'RX breaks', (Ann.RX_BREAK,)),
        ('rx-packets', 'RX packets', (Ann.RX_PACKET,)),
        ('tx-data-bits', 'TX bits', (Ann.TX_DATA_BIT,)),
        ('tx-data-vals', 'TX data', (Ann.TX_DATA, Ann.TX_START, Ann.TX_PARITY_OK, Ann.TX_PARITY_ERR, Ann.TX_STOP)),
        ('tx-warnings', 'TX warnings', (Ann.TX_WARN,)),
        ('tx-breaks', 'TX breaks', (Ann.TX_BREAK,)),
        ('tx-packets', 'TX packets', (Ann.TX_PACKET,)),
    )
    binary = (
        ('rx', 'RX dump'),
        ('tx', 'TX dump'),
        ('rxtx', 'RX/TX dump'),
    )
    idle_state = ['WAIT FOR START BIT', 'WAIT FOR START BIT']

    def putx(self, rxtx, data):
        s, halfbit = self.startsample[rxtx], self.bit_width / 2.0
        self.put(s - floor(halfbit), self.samplenum + ceil(halfbit), self.out_ann, data)

    def putx_packet(self, rxtx, data):
        s, halfbit = self.ss_packet[rxtx], self.bit_width / 2.0
        self.put(s - floor(halfbit), self.samplenum + ceil(halfbit), self.out_ann, data)

    def putpx(self, rxtx, data):
        s, halfbit = self.startsample[rxtx], self.bit_width / 2.0
        self.put(s - floor(halfbit), self.samplenum + ceil(halfbit), self.out_python, data)

    def putg(self, data):
        s, halfbit = self.samplenum, self.bit_width / 2.0
        self.put(s - floor(halfbit), s + ceil(halfbit), self.out_ann, data)

    def putp(self, data):
        s, halfbit = self.samplenum, self.bit_width / 2.0
        self.put(s - floor(halfbit), s + ceil(halfbit), self.out_python, data)

    def putgse(self, ss, es, data):
        self.put(ss, es, self.out_ann, data)

    def putpse(self, ss, es, data):
        self.put(ss, es, self.out_python, data)

    def putbin(self, rxtx, data):
        s, halfbit = self.startsample[rxtx], self.bit_width / 2.0
        self.put(s - floor(halfbit), self.samplenum + ceil(halfbit), self.out_binary, data)

    def __init__(self):
        self.reset()

    def reset(self):
        self.samplerate = None
        self.frame_start = [-1, -1]
        self.frame_valid = [None, None]
        self.cur_frame_bit = [None, None]
        self.startbit = [-1, -1]
        self.cur_data_bit = [0, 0]
        self.datavalue = [0, 0]
        self.paritybit = [-1, -1]
        self.stopbits = [[], []]
        self.startsample = [-1, -1]
        self.state = ['WAIT FOR START BIT', 'WAIT FOR START BIT']
        self.databits = [[], []]
        self.break_start = [None, None]
        self.packet_cache = [[], []]
        self.ss_packet, self.es_packet = [None, None], [None, None]
        self.idle_start = [None, None]

    def start(self):
        self.out_python = self.register(srd.OUTPUT_PYTHON)
        self.out_binary = self.register(srd.OUTPUT_BINARY)
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.bw = (self.options['data_bits'] + 7) // 8

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value
            self.bit_width = float(self.samplerate) / float(self.options['baudrate'])

    def get_sample_point(self, rxtx, bitnum):
        perc = self.options['sample_point'] or 50
        if not perc or perc not in range(1, 100):
            perc = 50
        perc /= 100.0
        bitpos = (self.bit_width - 1) * perc
        bitpos += self.frame_start[rxtx]
        bitpos += bitnum * self.bit_width
        return bitpos

    def wait_for_start_bit(self, rxtx, signal):
        self.frame_start[rxtx] = self.samplenum
        self.frame_valid[rxtx] = True
        self.cur_frame_bit[rxtx] = 0
        self.advance_state(rxtx, signal)

    def get_start_bit(self, rxtx, signal):
        self.startbit[rxtx] = signal
        self.cur_frame_bit[rxtx] += 1

        if self.startbit[rxtx] != 0:
            self.putp(['INVALID STARTBIT', rxtx, self.startbit[rxtx]])
            self.putg([Ann.RX_WARN + rxtx, ['Frame error', 'Frame err', 'FE']])
            self.frame_valid[rxtx] = False
            es = self.samplenum + ceil(self.bit_width / 2.0)
            self.putpse(self.frame_start[rxtx], es, ['FRAME', rxtx,
                (self.datavalue[rxtx], self.frame_valid[rxtx])])
            self.advance_state(rxtx, signal, fatal = True, idle = es)
            return

        self.cur_data_bit[rxtx] = 0
        self.datavalue[rxtx] = 0
        self.paritybit[rxtx] = -1
        self.stopbits[rxtx].clear()
        self.startsample[rxtx] = -1
        self.databits[rxtx].clear()

        self.putp(['STARTBIT', rxtx, self.startbit[rxtx]])
        self.putg([Ann.RX_START + rxtx, ['Start bit', 'Start', 'S']])

        self.advance_state(rxtx, signal)

    def handle_packet(self, rxtx):
        d = 'rx' if (rxtx == RX) else 'tx'
        delim = self.options[d + '_packet_delim']
        plen = self.options[d + '_packet_len']
        if delim == -1 and plen == -1:
            return

        if len(self.packet_cache[rxtx]) == 0:
            self.ss_packet[rxtx] = self.startsample[rxtx]
        self.packet_cache[rxtx].append(self.datavalue[rxtx])
        if self.datavalue[rxtx] == delim or len(self.packet_cache[rxtx]) == plen:
            self.es_packet[rxtx] = self.samplenum
            s = ''
            for b in self.packet_cache[rxtx]:
                s += self.format_value(b)
                if self.options['format'] != 'ascii':
                    s += ' '
            if self.options['format'] != 'ascii' and s[-1] == ' ':
                s = s[:-1]
            self.putx_packet(rxtx, [Ann.RX_PACKET + rxtx, [s]])
            self.packet_cache[rxtx] = []

    def get_data_bits(self, rxtx, signal):
        if self.startsample[rxtx] == -1:
            self.startsample[rxtx] = self.samplenum

        self.putg([Ann.RX_DATA_BIT + rxtx, ['%d' % signal]])

        s, halfbit = self.samplenum, int(self.bit_width / 2)
        self.databits[rxtx].append([signal, s - halfbit, s + halfbit])
        self.cur_frame_bit[rxtx] += 1

        self.cur_data_bit[rxtx] += 1
        if self.cur_data_bit[rxtx] < self.options['data_bits']:
            return

        bits = [b[0] for b in self.databits[rxtx]]
        if self.options['bit_order'] == 'msb-first':
            bits.reverse()
        self.datavalue[rxtx] = bitpack(bits)
        self.putpx(rxtx, ['DATA', rxtx,
            (self.datavalue[rxtx], self.databits[rxtx])])

        b = self.datavalue[rxtx]
        formatted = self.format_value(b)
        if formatted is not None:
            self.putx(rxtx, [rxtx, [formatted]])

        bdata = b.to_bytes(self.bw, byteorder='big')
        self.putbin(rxtx, [Bin.RX + rxtx, bdata])
        self.putbin(rxtx, [Bin.RXTX, bdata])

        self.handle_packet(rxtx)
        self.databits[rxtx] = []
        self.advance_state(rxtx, signal)

    def format_value(self, v):
        fmt, bits = self.options['format'], self.options['data_bits']

        if fmt == 'ascii':
            if v in range(32, 126 + 1):
                return chr(v)
            hexfmt = "[{:02X}]" if bits <= 8 else "[{:03X}]"
            return hexfmt.format(v)

        if fmt == 'dec':
            return "{:d}".format(v)

        if fmt == 'hex':
            digits = (bits + 4 - 1) // 4
            fmtchar = "X"
        elif fmt == 'oct':
            digits = (bits + 3 - 1) // 3
            fmtchar = "o"
        elif fmt == 'bin':
            digits = bits
            fmtchar = "b"
        else:
            fmtchar = None
            
        if fmtchar is not None:
            fmt = "{{:0{:d}{:s}}}".format(digits, fmtchar)
            return fmt.format(v)

        return None

    def get_parity_bit(self, rxtx, signal):
        self.paritybit[rxtx] = signal
        self.cur_frame_bit[rxtx] += 1

        if parity_ok(self.options['parity'], self.paritybit[rxtx],
                     self.datavalue[rxtx], self.options['data_bits']):
            self.putp(['PARITYBIT', rxtx, self.paritybit[rxtx]])
            self.putg([Ann.RX_PARITY_OK + rxtx, ['Parity bit', 'Parity', 'P']])
        else:
            self.putp(['PARITY ERROR', rxtx, (0, 1)])
            self.putg([Ann.RX_PARITY_ERR + rxtx, ['Parity error', 'Parity err', 'PE']])
            self.frame_valid[rxtx] = False

        self.advance_state(rxtx, signal)

    def get_stop_bits(self, rxtx, signal):
        self.stopbits[rxtx].append(signal)
        self.cur_frame_bit[rxtx] += 1

        if signal != 1:
            self.putp(['INVALID STOPBIT', rxtx, signal])
            self.putg([Ann.RX_WARN + rxtx, ['Frame error', 'Frame err', 'FE']])
            self.frame_valid[rxtx] = False

        self.putp(['STOPBIT', rxtx, signal])
        self.putg([Ann.RX_STOP + rxtx, ['Stop bit', 'Stop', 'T']])

        if len(self.stopbits[rxtx]) < self.options['stop_bits']:
            return
        self.advance_state(rxtx, signal)

    def advance_state(self, rxtx, signal = None, fatal = False, idle = None):
        frame_end = self.frame_start[rxtx] + self.frame_len_sample_count
        if idle is not None:
            self.idle_start[rxtx] = idle
        if fatal:
            self.state[rxtx] = 'WAIT FOR START BIT'
            return
            
        if self.state[rxtx] == 'WAIT FOR START BIT':
            self.state[rxtx] = 'GET START BIT'
            return
        if self.state[rxtx] == 'GET START BIT':
            self.state[rxtx] = 'GET DATA BITS'
            return
        if self.state[rxtx] == 'GET DATA BITS':
            self.state[rxtx] = 'GET PARITY BIT'
            if self.options['parity'] != 'none':
                return
        if self.state[rxtx] == 'GET PARITY BIT':
            self.state[rxtx] = 'GET STOP BITS'
            if self.options['stop_bits']:
                return
        if self.state[rxtx] == 'GET STOP BITS':
            ss = self.frame_start[rxtx]
            es = self.samplenum + ceil(self.bit_width / 2.0)
            self.handle_frame(rxtx, ss, es)
            self.state[rxtx] = 'WAIT FOR START BIT'
            self.idle_start[rxtx] = frame_end
            return
            
        self.state[rxtx] = 'WAIT FOR START BIT'

    def handle_frame(self, rxtx, ss, es):
        self.putpse(ss, es, ['FRAME', rxtx,
            (self.datavalue[rxtx], self.frame_valid[rxtx])])

    def handle_idle(self, rxtx, ss, es):
        self.putpse(ss, es, ['IDLE', rxtx, 0])

    def handle_break(self, rxtx, ss, es):
        self.putpse(ss, es, ['BREAK', rxtx, 0])
        self.putgse(ss, es, [Ann.RX_BREAK + rxtx,
                ['Break condition', 'Break', 'Brk', 'B']])
        self.state[rxtx] = 'WAIT FOR START BIT'

    def get_wait_cond(self, rxtx, inv):
        state = self.state[rxtx]
        if state == 'WAIT FOR START BIT':
            return {rxtx: 'r' if inv else 'f'}
        if state in ('GET START BIT', 'GET DATA BITS',
                'GET PARITY BIT', 'GET STOP BITS'):
            bitnum = self.cur_frame_bit[rxtx]
            want_num = ceil(self.get_sample_point(rxtx, bitnum))
            return {'skip': want_num - self.samplenum}

    def get_idle_cond(self, rxtx, inv):
        if self.idle_start[rxtx] is None:
            return None
        end_of_frame = self.idle_start[rxtx] + self.frame_len_sample_count
        if end_of_frame < self.samplenum:
            return None
        return {'skip': end_of_frame - self.samplenum}

    def inspect_sample(self, rxtx, signal, inv):
        if inv:
            signal = not signal

        state = self.state[rxtx]
        if state == 'WAIT FOR START BIT':
            self.wait_for_start_bit(rxtx, signal)
        elif state == 'GET START BIT':
            self.get_start_bit(rxtx, signal)
        elif state == 'GET DATA BITS':
            self.get_data_bits(rxtx, signal)
        elif state == 'GET PARITY BIT':
            self.get_parity_bit(rxtx, signal)
        elif state == 'GET STOP BITS':
            self.get_stop_bits(rxtx, signal)

    def inspect_edge(self, rxtx, signal, inv):
        if inv:
            signal = not signal
        if not signal:
            self.break_start[rxtx] = self.samplenum
            return
            
        if self.break_start[rxtx] is None:
            return
        diff = self.samplenum - self.break_start[rxtx]
        if diff >= self.break_min_sample_count:
            ss, es = self.frame_start[rxtx], self.samplenum
            self.handle_break(rxtx, ss, es)
        self.break_start[rxtx] = None

    def inspect_idle(self, rxtx, signal, inv):
        if inv:
            signal = not signal
        if not signal:
            self.idle_start[rxtx] = None
            return
            
        if self.idle_start[rxtx] is None:
            self.idle_start[rxtx] = self.samplenum
        diff = self.samplenum - self.idle_start[rxtx]
        if diff < self.frame_len_sample_count:
            return
            
        ss, es = self.idle_start[rxtx], self.samplenum
        self.handle_idle(rxtx, ss, es)
        self.idle_start[rxtx] = es

    def decode(self):
        if not self.samplerate:
            raise SamplerateError('Cannot decode without samplerate.')

        has_pin = [self.has_channel(ch) for ch in (RX, TX)]
        if not True in has_pin:
            raise ChannelError('Need at least one of TX or RX pins.')

        opt = self.options
        inv = [opt['invert_rx'] == 'yes', opt['invert_tx'] == 'yes']
        cond_data_idx = [None] * len(has_pin)

        frame_samples = 1
        frame_samples += self.options['data_bits']
        frame_samples += 0 if self.options['parity'] == 'none' else 1
        frame_samples += self.options['stop_bits']
        frame_samples *= self.bit_width
        self.frame_len_sample_count = ceil(frame_samples)
        self.break_min_sample_count = self.frame_len_sample_count
        cond_edge_idx = [None] * len(has_pin)
        cond_idle_idx = [None] * len(has_pin)

        while True:
            conds = []
            if has_pin[RX]:
                cond_data_idx[RX] = len(conds)
                conds.append(self.get_wait_cond(RX, inv[RX]))
                cond_edge_idx[RX] = len(conds)
                conds.append({RX: 'e'})
                cond_idle_idx[RX] = None
                idle_cond = self.get_idle_cond(RX, inv[RX])
                if idle_cond:
                    cond_idle_idx[RX] = len(conds)
                    conds.append(idle_cond)
            if has_pin[TX]:
                cond_data_idx[TX] = len(conds)
                conds.append(self.get_wait_cond(TX, inv[TX]))
                cond_edge_idx[TX] = len(conds)
                conds.append({TX: 'e'})
                cond_idle_idx[TX] = None
                idle_cond = self.get_idle_cond(TX, inv[TX])
                if idle_cond:
                    cond_idle_idx[TX] = len(conds)
                    conds.append(idle_cond)
            (rx, tx) = self.wait(conds)
            
            if cond_data_idx[RX] is not None and self.matched[cond_data_idx[RX]]:
                self.inspect_sample(RX, rx, inv[RX])
            if cond_edge_idx[RX] is not None and self.matched[cond_edge_idx[RX]]:
                self.inspect_edge(RX, rx, inv[RX])
                self.inspect_idle(RX, rx, inv[RX])
            if cond_idle_idx[RX] is not None and self.matched[cond_idle_idx[RX]]:
                self.inspect_idle(RX, rx, inv[RX])
                
            if cond_data_idx[TX] is not None and self.matched[cond_data_idx[TX]]:
                self.inspect_sample(TX, tx, inv[TX])
            if cond_edge_idx[TX] is not None and self.matched[cond_edge_idx[TX]]:
                self.inspect_edge(TX, tx, inv[TX])
                self.inspect_idle(TX, tx, inv[TX])
            if cond_idle_idx[TX] is not None and self.matched[cond_idle_idx[TX]]:
                self.inspect_idle(TX, tx, inv[TX])


# App-facing decoder helpers. app.py calls these functions directly and expects
# packets shaped as {"start": sample_index, "end": sample_index, "text": label}.
def _as_bits(samples):
    return [int(v) & 1 for v in samples]


def _edges(bits):
    return [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]


def _packet(start, end, text):
    return {"start": int(start), "end": int(max(end, start + 1)), "text": str(text)}


def decode_pwm(channel_data, fs):
    """Return PWM duty/period labels for a single digital channel."""
    bits = _as_bits(channel_data)
    if len(bits) < 3 or fs <= 0:
        return []

    all_edges = _edges(bits)
    rising = [i for i in all_edges if bits[i - 1] == 0 and bits[i] == 1]
    packets = []
    for a, b in zip(rising, rising[1:]):
        falling = next((e for e in all_edges if a < e < b and bits[e - 1] == 1 and bits[e] == 0), None)
        if falling is None:
            continue
        period = b - a
        high = falling - a
        duty = 100.0 * high / period
        freq = fs / period
        packets.append(_packet(a, b, f"PWM {duty:.1f}% {freq:.0f}Hz"))
        if len(packets) >= 80:
            break
    return packets


def decode_uart(channel_data, fs, baudrate):
    """Decode 8N1 UART on one RX line, idle high, LSB first."""
    bits = _as_bits(channel_data)
    if len(bits) < 12 or fs <= 0 or baudrate <= 0:
        return []

    bit_samples = fs / float(baudrate)
    if bit_samples < 2:
        return []

    packets = []
    i = 1
    while i < len(bits) - int(10 * bit_samples):
        if bits[i - 1] == 1 and bits[i] == 0:
            start = i
            value = 0
            ok = True
            for bit_no in range(8):
                sample_at = int(round(start + (1.5 + bit_no) * bit_samples))
                if sample_at >= len(bits):
                    ok = False
                    break
                value |= bits[sample_at] << bit_no
            stop_at = int(round(start + 9.5 * bit_samples))
            if ok and stop_at < len(bits):
                stop_ok = bits[stop_at] == 1
                char = chr(value) if 32 <= value <= 126 else "."
                suffix = "" if stop_ok else " FE"
                packets.append(_packet(start, stop_at, f"UART 0x{value:02X} '{char}'{suffix}"))
                i = int(start + 10 * bit_samples)
                if len(packets) >= 120:
                    break
                continue
        i += 1
    return packets


def decode_spi(clk, mosi, miso, fs):
    """Decode SPI mode 0 bytes on rising CLK edges."""
    clk_bits = _as_bits(clk)
    mosi_bits = _as_bits(mosi)
    miso_bits = _as_bits(miso)
    n = min(len(clk_bits), len(mosi_bits), len(miso_bits))
    if n < 2:
        return []

    packets = []
    word_start = None
    mosi_val = 0
    miso_val = 0
    bit_count = 0
    for i in range(1, n):
        if clk_bits[i - 1] == 0 and clk_bits[i] == 1:
            if bit_count == 0:
                word_start = i
                mosi_val = 0
                miso_val = 0
            mosi_val = (mosi_val << 1) | mosi_bits[i]
            miso_val = (miso_val << 1) | miso_bits[i]
            bit_count += 1
            if bit_count == 8:
                packets.append(_packet(word_start, i, f"SPI MOSI:{mosi_val:02X} MISO:{miso_val:02X}"))
                bit_count = 0
                if len(packets) >= 120:
                    break
    return packets


def decode_i2c(scl, sda, fs):
    """Decode common 7-bit I2C transfers from SCL/SDA samples."""
    scl_bits = _as_bits(scl)
    sda_bits = _as_bits(sda)
    n = min(len(scl_bits), len(sda_bits))
    if n < 3:
        return []

    packets = []
    in_frame = False
    byte_bits = []
    byte_start = 0
    byte_index = 0

    for i in range(1, n):
        scl_high = scl_bits[i] == 1 and scl_bits[i - 1] == 1
        if scl_high and sda_bits[i - 1] == 1 and sda_bits[i] == 0:
            packets.append(_packet(i, i + 1, "I2C START"))
            in_frame = True
            byte_bits = []
            byte_index = 0
            continue
        if scl_high and sda_bits[i - 1] == 0 and sda_bits[i] == 1:
            packets.append(_packet(i, i + 1, "I2C STOP"))
            in_frame = False
            byte_bits = []
            continue
        if not in_frame or not (scl_bits[i - 1] == 0 and scl_bits[i] == 1):
            continue

        if not byte_bits:
            byte_start = i
        byte_bits.append(sda_bits[i])
        if len(byte_bits) < 9:
            continue

        value = 0
        for bit in byte_bits[:8]:
            value = (value << 1) | bit
        ack = "ACK" if byte_bits[8] == 0 else "NACK"
        if byte_index == 0:
            addr = value >> 1
            rw = "R" if value & 1 else "W"
            text = f"I2C {addr:02X} {rw} {ack}"
        else:
            text = f"I2C {value:02X} {ack}"
        packets.append(_packet(byte_start, i, text))
        byte_bits = []
        byte_index += 1
        if len(packets) >= 160:
            break
    return packets
