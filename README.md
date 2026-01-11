# seplos-bms-console
Monitor Seplos BMS from the console. Use the command line to read and write BMS parameters.

This makes it possible both to view BMS information such as cell
voltages and temperatures, and also to set BMS parameters including
battery capacity and charging and protection voltages.

It will also broadcast battery info on mqtt to a server of your choice,
along with homeassistant autodiscovery info.

## Warning

Fiddling with your BMS is always dangerous. Wrong settings can permanently
damage cells, or start a fire and damage other things. 

Using unofficial scripts (like these) carries additional risk. If something
goes wrong it could brick your BMS.

Modbus works by using a series of registers which we can write to. If one of
these is mis-assigned in the program (either a bug, or because Seplos changed
their protocols) then we could be writing a different parameter from what we
think we are. If this happened you could for example think you were setting
maximum charge current to 60A, when you actually set bulk charge voltage to
60V. This is unlikely, but not impossible. Check you're happy with how your
BMS is behaving, and test it is doing what it should before proceeding.

## Quick Start

1. Connect your computer to the BMS using a RS485 to serial / usb adaptor.
This needs to go to the correct port on the BMS (not the Canbus one).

Edit seplos.ini to have the address and details of your MQTT server 
(if you have one) and your usb adaptor.

2. Install requirements - preferably in a virtual environment. If you don't
intend to run the interactive console then no need to install the textual
packages.
```bash
python -m venv .venv
. .venv/bin/activate
git clone https://github.com/ivanol/seplos3-console-mqtt
cd seplos3-console-mqtt
pip install pyserial pyserial-asyncio paho-mqtt textual textual-autocomplete
```
3. Edit seplos.ini to contain details of your mqtt server (if you have one)
and the USB adaptor.

4. Either run *./seplos_console.py* to get an interactive console which uses
textual to give a nice status dashboard of your battery, run
*./seplos.py* to get mirroring of data to mqtt/Homeassistant, or run *./seplos.py*
with command arguments to script reading and setting battery parameters..

## Usage
*seplos.py* can be run without any command line parameters, in which case it
reads BMS values continuously and sends them to Homeassistant by MQTT.

*seplos.py* can also be run with arguments (*-h* will give you a list). For example:
```bash
$ ./seplos.py -s battery_low_voltage_alarm
battery_low_voltage_alarm 46.4 V
$ ./seplos.py -e battery_low_voltage_alarm 45

We will change battery_low_voltage_alarm from 46.4 V to 45.0 V

If this looks right and you wish to go ahead then type 'yes'. All other values abort: yes
Update sent over modbus
Write completed. New value of battery_low_voltage_alarm is confirmed as 45.0 V
```

For seplos_console either watch the screen, or if you want to edit then 
press *t* to see the list of editable parameters. If you type a param
name followed by its new value in the input field it should update. 

Very much a work in progress, but hopefully a useful starting point for
someone.

## Known Issues
* Doesn't work with multiple batteries.
