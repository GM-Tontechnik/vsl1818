import sys
import re
import datetime
import struct
from socket import *
import wsgiref.simple_server
import wsgiref.util
import threading
import traceback
import time
import json

HOST='localhost'
PORT=7069

MAX_CHANNEL=7 # ignore higher channels: they're SPIDF, ADAT, and DAW

def parsestr(s, n=None):
    assert '\x00' in s
    if n is not None:
        assert len(s) == n
    return s[:s.find('\x00')]

# control_id -> control_name
control_decode = {
    3000: "master",
    1: "master pan",
    3005: "aux 3-4",
    16: "pan 3-4",
    3007: "aux 5-6",
    17: "pan 5-6",
    3009: "aux 7-8",
    18: "pan 7-8",
    3013: "A",
    3014: "B",
    3052: "mute",
    62: "phase reverse",
    3021: "high pass",
    3063: "post",
    }

class VSL1818(object):
    def __init__(self):
        self.loaded = False
        self.killed = False
        self.connection = None
        self.levels = None
        self.channel_names = {} # channel_id -> channel_name
        self.channel_id_strs = {} # channel_id -> channel_id_str

        # channel_id -> control_id -> value
        self.channels = {}

    def update(self, message_header, message_body):
        unknown1, unknown2, category, unknown3 = struct.unpack("IIHH", message_header)
        assert unknown1 == 0x01020103
        assert unknown2 == 1234
        assert unknown3 == 10

        if category == 5:
            self.loaded = True # we're not fully loaded until we get some levels
            self.levels = struct.unpack("128B", message_body)

        elif category == 4:
            unknown4, channel_id, channel_name = struct.unpack("HH48s", message_body)
            assert unknown4 == 0
            if channel_id > MAX_CHANNEL:
                return
            self.channel_names[channel_id] = parsestr(channel_name)

        elif category == 2:
            control_id, value, channel_id_str = struct.unpack("=Hd32s", message_body)
            channel_id_str = parsestr(channel_id_str)
            if channel_id_str.startswith("fx "):
                return
            assert channel_id_str.startswith("in")
            channel_id = int(channel_id_str.split(",")[0][2:])
            if channel_id > MAX_CHANNEL:
                return
            if channel_id not in self.channels:
                self.channels[channel_id] = {}
                self.channel_id_strs[channel_id] = channel_id_str

            if control_id not in control_decode:
                return # ignoring unknown control_ids for now

            self.channels[channel_id][control_id] = value

        else:
            raise Exception("unknown category %s" % category)

    def update_always(self):
        assert self.connection

        while not self.killed:
            signature, l = struct.unpack("II", self.read(8))
            assert signature == 0xaa550011
            v = self.read(l)
            self.update(v[:12], v[12:])

    def dumpstate(self):
        if not self.loaded:
            return "<p>loading...</p>"

        s = ["<table border=1>"]
        for channel_id, controls in sorted(self.channels.iteritems()):
            if channel_id not in self.channel_names:
                continue # ignoring unknown channel_ids for now
            channel_name = self.channel_names[channel_id]
            s.append("<tr><td rowspan=%s>%s" % (len(controls), channel_name))
            for control_id, value in sorted(controls.iteritems()):
                if not s[-1].startswith("<tr>"):
                    s.append("<tr>")
                s.append("<td>%s<td>%s" % (control_decode[control_id], value))
        s.append("</table>")
        return "".join(s)

    def read(self, n):
        s = ""
        def remaining():
            return n-len(s)
        while remaining() > 0:
            b = self.connection.recv(remaining())
            s += b
        return s

    def connect(self):
        assert not self.connection

        self.connection = socket(AF_INET, SOCK_STREAM)
        self.connection.connect((HOST, PORT))

        # TODO: send our real mac address
        message = struct.pack("IIHH32s32s",
                              0x01020103,
                              1234,
                              0x0003,
                              10,
                              'C8:BC:C8:1B:9F:0A',
                              'AB1818-VSL')
        self.connection.send(struct.pack("II", 0xaa550011, len(message)))
        self.connection.send(message)

def index(vsl):
    s = ["<html>", "<h1>VSL 1818</h1>"]
    for text, target in [["Dump State", "/dump"],
                         ["List Controls", "/controls"],
                         ["List Channels", "/channels"]]:
        s.append('<a href="%s">%s</a><br>' % (target, text))
    return "".join(s)

def list_controls(vsl):
    s = ["<html>", "<h1>Available Controls</h1>"]
    for control_id, control_name in sorted(control_decode.iteritems()):
        s.append('<a href="/control/%s">%s</a><br>' % (control_id, control_name))
    return "".join(s)

def list_channels(vsl):
    s = ["<html>", "<h1>Available Channels</h1>"]
    for channel_id in sorted(vsl.channels):
        if channel_id not in vsl.channel_names:
            continue # ignoring unknown channel_ids for now
        channel_name = vsl.channel_names[channel_id]
        s.append('<a href="/channel/%s">%s</a><br>' % (channel_id, channel_name))
    return "".join(s)

CLICK_HANDLER=("<script>"
               "function click_handler(fg, bg, channel_id_str, control_id) {"
               "  return function(e) {"
               "     var value = e.offsetX/bg.offsetWidth;"
               "     /* todo: send to server via ajax */"
               "     console.debug('channel='+channel_id_str + ' control='+control_id + ' value='+value);"
               "  }"
               "}"
               "</script>")

XML_HTTP_REQUEST = (
    "<script>"
    "function loadAjax(method, request, callback) {"
    "  var xr;"
    "  if (window.XMLHttpRequest) {"
    "    /* modern browsers */"
    "    xr = new XMLHttpRequest();"
    "  } else {"
    "    /* IE6, IE5 */"
    "    xr = new ActiveXObject('Microsoft.XMLHTTP');"
    "  }"
    "  xr.onreadystatechange=(function() {"
    "    if (xr.readyState==4 && xr.status==200) {"
    "      callback(xr.responseText);"
    "    }"
    "  });"
    "  xr.open(method, request, true);"
    "  xr.send();"
    "}"
    "</script>")

def show_sliders(title, vsl, slider_ids):
    s = ["<html>", "<h1>%s</h1>" % title]

    s.append("<style>"
             ".barfg{background-color:black;margin:0;padding:0}"
             ".barbg{background-color:#BBB;margin:0;padding:0}"
             "</style>")

    # call this in callback
    #fg: style="width:%.2f%%"' % value*100

    s.append("<dl>")
    for slider_name, channel_id, control_id in slider_ids:
        s.append('<dt>%s<dd>'
                 '<div id="slider-bg-%s-%s" class="barbg">'
                 '  <div id="slider-fg-%s-%s" class="barfg">'
                 '    &nbsp;</div></div>'
                 % (slider_name, channel_id, control_id, channel_id, control_id))
    s.append("</dl>")

    s.append(XML_HTTP_REQUEST)
    s.append(CLICK_HANDLER)
    s.append("<script>"
             "var sliders=%s;" # channel_id, control_id, channel_id_str
             "var i = 0;"
             "for (i = 0 ; i < sliders.length ; i++) {"
             "  var channel_id = sliders[i][0];"
             "  var control_id = sliders[i][1];"
             "  var channel_id_str = sliders[i][2];"
             "  var bg = document.getElementById('slider-bg-' + channel_id"
             "                                          + '-' + control_id);"
             "  var fg = document.getElementById('slider-fg-' + channel_id"
             "                                          + '-' + control_id);"
             "  bg.onclick = click_handler(fg, bg, channel_id_str, control_id);"
             "}"
             "</script>" % 
             json.dumps([(channel_id, control_id, vsl.channel_id_strs[channel_id])
                         for slider_name, channel_id, control_id in slider_ids]))
    return "\n".join(s)

def show_control(request, vsl):
    (control_raw,) = request
    control_id = int(control_raw)
    control_name = control_decode[control_id]

    sliders = [(vsl.channel_names[channel_id], channel_id, control_id)
               for channel_id, controls in sorted(vsl.channels.items())
               if control_id in controls]
    return show_sliders("Control %s" % control_name, vsl, sliders)

def show_channel(request, vsl):
    (channel_id_raw,) = request
    channel_id = int(channel_id_raw)
    channel_name = vsl.channel_names[channel_id]

    controls = vsl.channels[channel_id]
    sliders = [(control_decode[control_id], channel_id, control_id)
               for control_id in sorted(controls)]
    return show_sliders("Channel %s" % channel_name, vsl, sliders)

def handle_request(request, vsl):
    assert not request[0]
    request = request[1:]

    if not request[0]:
        return index(vsl)
    if request == ['channels']:
        return list_channels(vsl)
    if request[0] == "channel":
        return show_channel(request[1:], vsl)
    if request == ['controls']:
        return list_controls(vsl)
    if request[0] == "control":
        return show_control(request[1:], vsl)
    if request == ['dump']:
        return vsl.dumpstate()
    raise Exception("Unknown Request Path: '%s'" % request)

class MockVSL():
    def __init__(self):
        self.loaded = False
        self.killed = False
        self.levels = None
        self.channel_names = {1: "main", 2: "aux"}

        # channel_id -> control_id -> value
        self.channels = {1:{}, 2:{}}
        self.channel_id_strs = {1:"in1,0,2", 2:"in2,0,2"}

    def update_always(self):
        i = 0
        while not self.killed:
            c = float(i % 256)

            self.levels = (c, c, 256-c, 256-c)
            self.channels[1][3000] = 0.0
            self.channels[1][3005] = .1
            self.channels[1][3007] = .5
            self.channels[1][3009] = .9
            self.channels[1][1] = 1.0
            self.channels[2][3000] = 0.1
            self.channels[2][3005] = .2
            self.channels[2][3007] = .3
            self.channels[2][3009] = .4
            self.channels[2][1] = .5

            time.sleep(1)
            i += 1

    def dumpstate(self):
        return "dumpstate"

def start(args):
    if len(args) == 1 and args[0] == "mock":
        vsl = MockVSL()
    else:
        assert not args

        vsl = VSL1818()
        vsl.connect()

    update_thread = threading.Thread(target=vsl.update_always)
    update_thread.start()

    def vsl_app(environ, start_response):
        try:
            response = handle_request(environ["PATH_INFO"].split("/"), vsl)
            headers = [('Content-type', 'text/html')]
            start_response("200 OK", headers)
            return str(response)
        except Exception:
            tb = traceback.format_exc()
            headers = [('Content-type', 'text/plain')]
            start_response("500 Internal Server Error", headers)
            return tb

    httpd = wsgiref.simple_server.make_server('', 8000, vsl_app)
    print "Serving HTTP on port 8000..."

    try:
        httpd.serve_forever()
    except:
        vsl.killed = True
        raise

if __name__ == "__main__":
    start(sys.argv[1:])
