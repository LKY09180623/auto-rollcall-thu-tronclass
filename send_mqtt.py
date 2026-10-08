import paho.mqtt.client as mqtt
import json
import time

MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
CHANNEL = "A113280040_secret"
PAYLOAD = "/j?p=0~2z1u!3~1781064158bd17c2ef08d5d426afa53998d92f6b00!4~vz0z"

def on_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker with result code " + str(rc))
    topic = f"tronclass/qr_relay/{CHANNEL}"
    client.publish(topic, PAYLOAD, qos=1)
    print(f"Published payload to {topic}")
    time.sleep(1)
    client.disconnect()

client = mqtt.Client()
client.on_connect = on_connect

client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_forever()
print("Done!")
