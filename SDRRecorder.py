import yaml
import sys
import paramiko
import re
import threading

CONFIGFILE = './config.yaml'


class SDRRecorder:
	def __init__(self):
		try:
			self.config = self.read_configuration_file(CONFIGFILE)
		except FileNotFoundError:
			print(f'Configuration file {CONFIGFILE} is not found.', file=sys.stderr)
			exit(-1)

		if not self.check_configuration(self.config):
			print(f"{CONFIGFILE} is invalid format.")
			exit(-1)
		self.client = paramiko.SSHClient()
		self.open_receivers(self.config, self.client)

	@staticmethod
	def read_configuration_file(file_name):
		with open(file_name, 'r') as yml:
			config = yaml.safe_load(yml)
		return config

	@staticmethod
	def check_configuration(config):
		if 'Host' not in config:
			return False

		host = config['Host']
		if 'ip_addr' not in host or 'Receivers' not in host:
			return False
		if not host['ip_addr']:
			return False

		receivers = host['Receivers']
		for receiver in receivers:
			if 'Receiver' not in receiver:
				return False
			rcv = receiver['Receiver']
			if 'port' not in rcv:
				return False
			if 'station_name' not in rcv:
				return False
			if 'freq' not in rcv:
				return False
			if 'mode' not in rcv:
				return False
			if 'additional_options' not in rcv:
				return False
			if not rcv['port'] or not rcv['station_name'] or not rcv['mode']:
				return False
		return True

	def open_receivers(self, config, client):
		host = config['Host']['ip_addr']
		user = config['Host']['user']
		password = config['Host']['password']

		client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
		client.load_host_keys(filename="/home/katsuwo/.ssh/known_hosts")
		client.connect(host, username=user, password=password)

		self.kill_all_rtl_fm_process(client)

		threds = []
		device_index = 0
		for receiver in config['Host']['Receivers']:
			freq = receiver['Receiver']['freq']
			mode = receiver['Receiver']['mode']
			opt = receiver['Receiver']['additional_options']
			port = receiver['Receiver']['port']
			cmdline = f"rtl_fm -f {freq} -M {mode} {opt} -d {device_index}|socat -u - TCP-LISTEN:{port}"
			device_index += 1
			threds.append(threading.Thread(target=self.execute_rtl_fm, args=([client, port, user, cmdline])))
		for th in threds:
			th.start()

#			self.execute_rtl_fm(client, port, user, cmdline)

	def execute_rtl_fm(self, client, port, user, cmdline):
		print(cmdline)
		stdin, stdout, stderr = client.exec_command(cmdline)
		for line in stdout:
			print(line)
		for error_line in stderr:
			if "Address already in use" in error_line:
				self.kill_others_process(client, port, user)
				self.execute_rtl_fm(client, port, user, cmdline)

	def kill_others_process(self, client, port, user):
		stdin, stdout, stderr = client.exec_command(f"lsof -i:{port}")
		linecount = 0
		for line in stdout:
			linecount+=1
			if linecount == 2:
				pid = line.split("socat ")[1].split(user)[0].replace(" ", "")
				killer = f"kill -9 {pid}"
				print(f"other process is using port {port}")
				print(f"kill other process PID:{pid} [ {killer} ]")
				client.exec_command(killer)
				break

	def kill_all_rtl_fm_process(self, client):
		stdin, stdout, stderr = client.exec_command(f"ps aux")
		for line in stdout:
			if 'rtl_fm ' in line:
				old_process = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
				killer = f"kill -9 {old_process}"
				print(f"kill old rtl_fm process { {killer} }")
				client.exec_command(killer)



if __name__ == '__main__':
	SDRRecorder()
