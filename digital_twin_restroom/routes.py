from flask import Blueprint, render_template

dt = Blueprint("dt", __name__)

@dt.route("/restroom")
def index():
    return render_template("restroom/index.html")

@dt.route("/restroom/dht11")
def dht11():
    return render_template("restroom/dht11.html")

@dt.route("/restroom/ds18b20")
def ds18b20():
    return render_template("restroom/DS18B20.html")

@dt.route("/restroom/ldr")
def ldr():
    return render_template("restroom/ldr.html")

@dt.route("/restroom/mqtt135")
def mqtt135():
    return render_template("restroom/MQTT135.html")

@dt.route("/restroom/esp32")
def esp32():
    return render_template("restroom/ESP32.html")
