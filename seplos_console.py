#!/usr/bin/env python

"""
Seplos BMSv3 Reader.

Running this runs an interactive textual based console for reviewing and querying seplos data

"""

import asyncio
import logging
import math
import sys
from enum import Enum

from textual.message import Message
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Button, Digits, Input, Label, RichLog, Markdown, TabbedContent, TabPane
from textual.suggester import Suggester
from textual.containers import HorizontalGroup, VerticalGroup, VerticalScroll
from textual_autocomplete import AutoComplete, DropdownItem, TargetState

from seplos import SeplosModbusMqttBridge, SeplosBattery

# We will use this to connect our app class up to logging.
# Takes incoming log messages, and sends to any class with a tlogger() method
class TextualLogHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.receiver = None

    def emit(self, record):
        if self.receiver != None:
            self.receiver.tlogger(record)
        else:
            print(record.getMessage())

log = logging.getLogger()
log.setLevel(logging.INFO)
textual_log_handler = TextualLogHandler()
textual_log_handler.setLevel(logging.DEBUG)
log.addHandler(textual_log_handler)

# A bunch of widgets for displaying data.
# These need to have a method called:
#     seplosUpdate(self, bid, k, v)
# and add the class "seplos_listener". They will be called
# with any updates from seplos modbus. k is the key
# (eg. "max_cell_voltage",) v is the value, and bid is the
# battery id. Need to ignore keys that are not relevant.

# Display multiple seplos fields in the format:
# **label_text** kval1 seperator kval2 ... kvaln suffix
class MultiFieldDisplay(HorizontalGroup):
    def __init__(self, label_txt: str, seperator: str, suffix: str, *keys):
        self.label_txt = label_txt
        self.seperator = seperator
        self.suffix = suffix
        self.keys = keys
        self.values = []
        self.label = None
        for i in range(len(keys)):
            self.values.append(None)
        HorizontalGroup.__init__(self)
        self.add_class("seplos_listener")

    def compose(self) -> ComposeResult:
        l =  Label(self.label_txt)
        l.add_class("bold_txt")
        yield l
        self.label = Label()
        yield self.label

    def redisplay(self):
        if self.label==None:
            return
        txt = ""
        for v in self.values:
            if len(txt) != 0:
                txt += self.seperator
            txt += str(v)
        txt += self.suffix
        self.label.update(txt)

    def seplosUpdate(self, bid, k, v):
        for i in range(len(self.keys)):
            if k == self.keys[i]:
                self.values[i] = v
                self.redisplay()

# Display a single seplos value in a big box with its unit.
# Used for SoC, voltage and current at present.
# color_cb is called with the value on each value change,
# and should return a color (FIXME - make this a style) for
# the display.
class ValueWithUnit(VerticalGroup):
    def __init__(self, name, value, unit: str, key: str, color_cb=None):
        VerticalGroup.__init__(self)
        self.label_name = name
        self.initial_value = str(value)
        self.unit_s = unit
        self.key = key
        self.color_cb = color_cb
        self.add_class("seplos_listener")

    def compose(self) -> ComposeResult:
        self.digits = Digits(str(self.initial_value))
        self.unit = Label(self.unit_s, id="unit")
        self.setColour(self.initial_value)
        yield Label(self.label_name)
        yield self.digits
        yield self.unit

    def setColour(self, value):
        if self.color_cb:
            self.styles.color = self.color_cb(float(value))

    def seplosUpdate(self, bid, key: str, value: float):
        if key==self.key:
            self.digits.update(str(value))
            self.setColour(value)

    # A simple static color callback
    def PosGrNegRed(value):
        if(value<0):
            return "red"
        if(value>0):
            return "green"
        return ""

# Display all cell voltages in a grid.
class Cells(VerticalGroup):
    def __init__(self, number_of_cells, rows=4) -> None:
        VerticalGroup.__init__(self)
        self.n = number_of_cells
        self.rows = rows
        self.cells = []
        self.voltages = []
        self.color_styles = []
        self.add_class("seplos_listener")
        self.average_cell_voltage = None

    def compose(self) -> ComposeResult:
        self.cells = []
        self.voltages = []
        self.color_styles = []
        cellNo = 0
        cells_per_row = math.ceil(self.n/self.rows)
        for y in range(self.rows):
            row = []
            for x in range(cells_per_row):
                if cellNo<self.n:
                    c = Label("_.___ ")
                    c.add_class("voltage_ok")
                    row.append(c)
                    self.cells.append(c)
                    self.voltages.append(None)
                    self.color_styles.append("voltage_ok")
                    cellNo += 1
            yield(HorizontalGroup(*row))

    def seplosUpdate(self, bid, key: str, value: float):
        if key=="average_cell_voltage":
            self.average_cell_voltage = float(value)
            self.updateColors()
        if key[:5]=="cell_":
            cellno = 0
            try:
                cellno = int(key[5:])
            except ValueError:
                return #  Not a voltage update
            if cellno > self.n:
                return
            self.voltages[cellno-1] = float(value)
            self.cells[cellno-1].update(f"{value:.3f} ")
            self.updateColors()

    def getCellColour(self, i) -> None:
        if self.voltages[i] == None:
            return "voltage_ok"
        if self.voltages[i] < 2.6 or self.voltages[i] > 3.6:
            return "orange"
        if self.voltages[i] < 2.9 or self.voltages[i] > 3.5:
            return "red"
        if self.average_cell_voltage == None:
            return "voltage_ok"
        diff = self.voltages[i] - self.average_cell_voltage
        if(diff < -0.001):
            return "red"
        if(diff > 0.001):
            return "voltage_high"
        return "voltage_ok"

    def updateColors(self) -> None:
        for i in range(self.n):
            color = self.getCellColour(i)
            if color != self.color_styles[i]:
                self.cells[i].add_class(color)
                self.cells[i].remove_class(self.color_styles[i])
                self.color_styles[i] = color
            # self.cells[i].styles.color = self.getCellColour(i)
            # self.cells[i].refresh()

# Show all writable params
class Params(VerticalScroll):
    def __init__(self) -> None:
        # This is horrible, but we want the list of possible fields
        # before we get any modbus data and set up our real batteries.
        fake_battery = SeplosBattery("", 1, None)
        self.write_fields = fake_battery.write_fields()
        VerticalScroll.__init__(self)

    def compose(self):
        wf = self.write_fields
        for i in range(0, len(wf), 2):
            row = [MultiFieldDisplay(f"{wf[i]}: ", "", "", wf[i])]
            if i+1 < len(wf):
                row.append(MultiFieldDisplay(f"{wf[i+1]}: ", "", "", wf[i+1]))
            yield HorizontalGroup(*row)

# A cmd line widget with autocomplete
class CmdLine(HorizontalGroup):
    class CmdSubmitted(Message):
        def __init__(self, cmd: str) -> None:
            super().__init__()
            self.cmd = cmd

    def __init__(self):
        fake_battery = SeplosBattery("", 1, None)
        self.read_fields = fake_battery.read_fields()
        self.write_fields = fake_battery.write_fields()

        HorizontalGroup.__init__(self)

    def compose(self) -> ComposeResult:
        self.text_input = Input(placeholder="parameter_name value.")
        yield self.text_input
        yield AutoComplete(
            self.text_input,
            candidates= self.candidates_callback # ["help", "h", "list", "set", "read"]
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value=="quit":
            exit()
        else:
            self.post_message(self.CmdSubmitted(event.value))
            self.text_input.value = ""

    def candidates_callback(self, state: TargetState) -> list[DropdownItem]:
        prefix = ""
        cmds = self.write_fields
        """
        field_cmds = ["set ", "read "]
        if state.text[0:5]=="read ":
            cmds = [ f"read {f}" for f in self.read_fields ]
        if state.text[0:4]=="set ":
            cmds = [ f"set {f}" for f in self.write_fields ]
        """
        return [
            DropdownItem(item, prefix=prefix)
            for item in cmds
        ]

# The console app
class SeplosConsole(App):
    CSS_PATH = "seplos_console.tcss"
    BINDINGS = [
                   ("d", "toggle_dark", "Toggle dark mode"),
                   ("t", "toggle_tab", "Switch tab"),
                   ("e", "edit_param", "Edit Parameter"),
                   ("q", "quit", "Quit"),
               ]

    def __init__(self, smmb = None):
        App.__init__(self)
        self.smmb = smmb
        self.current_battery = None
 
        if self.smmb != None:
            self.smmb.set_update_cb(self.receiveSeplosUpdate)

    async def on_mount(self) -> None:
        # We can only handle logging once we have somewhere to put the messages.
        textual_log_handler.receiver = self
        if self.smmb != None:
            self.run_worker(self.smmb.run_async())

    def LiFePO4volts16s(value):
        if value < 46:
            return "orange"
        if value < 48:
            return "red"
        if value < 55:
            return "green"
        if value < 58:
            return "red"
        return "orange"

    def LiFePO4cellVolts(value):
        if value < 2.6:
            return "orange"
        if value < 3:
            return "red"
        if value < 3.4:
            return "green"
        if value < 3.6:
            return "red"
        return "orange"

    def StateOfCharge(value):
        if value > 80:
            return "blue"
        if value > 60:
            return "green"
        if value > 40:
            return "white"
        if value > 20:
            return "red"
        return "orange"

    def compose(self) -> ComposeResult:
        headlines = HorizontalGroup(
            ValueWithUnit("Pack Volts", 0, "V", "pack_voltage", SeplosConsole.LiFePO4volts16s),
            ValueWithUnit("Current", 0, "A", "current", ValueWithUnit.PosGrNegRed),
            ValueWithUnit("SoC", 0, "%", "soc", SeplosConsole.StateOfCharge),
            Cells(16, 4),
        )
        widgets = [
            headlines,
            MultiFieldDisplay("Cell temps: ", " / ", " °C", "cell_temp_1", "cell_temp_2", "cell_temp_3", "cell_temp_4"),
            MultiFieldDisplay("Capacity left: ", " / ", " Ah", "remaining_capacity", "total_capacity"),
            MultiFieldDisplay("Total Discharge Capacity: ", "", " Ah", "total_discharge_capacity"), 
            MultiFieldDisplay("SOH / Cycles: ", "% / ", "", "soh", "cycles"), 
            MultiFieldDisplay("Max chg / dis cur: ", "A / ", "A", "maxchgcurt", "maxdiscurt"), 
            MultiFieldDisplay("Power: ", "", "W", "power"), 

            # We don't bother to display the following because they should be obvious from what is already there.
            # ("Average Cell Voltage", "average_cell_voltage"), 
            # ("Average Cell Temp", "average_cell_temp"), 
            # ("Max Cell Voltage", "max_cell_voltage"), 
            # ("Min Cell Voltage", "min_cell_voltage"), 
            # ("Max Cell Temp", "max_cell_temp"), 
            # ("Min Cell Temp", "min_cell_temp"), 
            # ("Cell Delta", "cell_delta"), 
        ]

        widgets.append(RichLog(highlight=True, markup=True))


        yield Header()
        yield Footer()
        self.cmdline = CmdLine()

        with TabbedContent(initial="monitor"):
            with TabPane("Monitor", id="monitor"):
                yield VerticalScroll(*widgets)
            with TabPane("Params", id="params"):
                yield VerticalGroup(self.cmdline, Params(), RichLog(highlight=True, markup=True))

    def action_toggle_dark(self) -> None:
        """An action to toggle dark mode."""
        self.theme = (
            "textual-dark" if self.theme == "textual-light" else "textual-light"
        )

    def action_toggle_tab(self) -> None:
        self.set_focus(None)  # Clear focus first. Otherwise tab switching fails erratically.
        content = self.query_one(TabbedContent)
        if content.active=="monitor":
            content.active="params"
        else:
            content.active="monitor"

    def action_quit(self) -> None:
        quit()

    def action_edit_param(self) -> None:
        self.cmdline.text_input.focus()

    def receiveSeplosUpdate(self, uid, k, value):
        for sl in self.query(".seplos_listener"):
            sl.seplosUpdate(uid, k, value)

    def tlogger(self, record: logging.LogRecord):
        color = {
            logging.DEBUG: "green",
            logging.INFO: "blue",
            logging.WARNING: "red",
            logging.ERROR: "red",
            logging.CRITICAL: "red"}.get(record.levelno, "white")
        for richlog in self.query(RichLog):
            richlog.write(f"[bold {color}]{record.getMessage()}")

    # Multiple batteries not currently supported as I don't have this
    # setup. To implement will need to edit this function, add filters
    # by battery number to either the receiveSeplosUpdate or
    # seplosUpdate functions, and add some sort of switching interface.
    def setCurrentBattery(self):
       if self.current_battery != None:
           return
       batteries = self.smmb.available_batteries()
       self.current_battery = batteries[0]

    def getCurrentBattery(self):
       self.setCurrentBattery()
       return self.smmb.getBattery(self.current_battery)

    def on_cmd_line_cmd_submitted(self, event: CmdLine.CmdSubmitted) -> None:
        cmds = event.cmd.split()
        if len(cmds)<2:
            return
        field = cmds[0]
        arg = cmds[1]
        
        argf = float(arg)
        if argf==0:
            log.warning(f"Temporarily disabling writing 0 to fields during testing - it's usually an error")
            return
        log.info(f"Set field {field} with {argf}")
        self.setCurrentBattery()
        self.smmb.writeFieldModbus(self.current_battery, field, argf)
        self.smmb.requestParams(self.smmb.modbus)

def runInteractive():
    smmb = SeplosModbusMqttBridge()
    app = SeplosConsole(smmb)
    app.run()

# Run without modbus for testing the console interface
def runTest():
    SeplosConsole().run()

if len(sys.argv)>1 and (sys.argv[1]=="--test" or sys.argv[1]=="-t"):
    runTest()
else:
    runInteractive()
