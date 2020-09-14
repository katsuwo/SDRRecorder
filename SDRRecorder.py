import yaml
import sys
import paramiko
import re
import threading
import time
import subprocess
import os

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

		if 'sock2wav' not in config:
			return False

		sock2wav = config['sock2wav']
		if 'path' not in sock2wav:
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
		known_hosts = config['hostkey']['known_hosts_file']

		# setup Paramiko ssh client
		client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#		client.load_host_keys(filename="/home/katsuwo/.ssh/known_hosts")
		client.load_host_keys(filename=known_hosts)
		client.connect(host, username=user, password=password)

		self.kill_all_rtl_fm_process(client)

		rtl_fm_threds = []
		device_index = 0
		for receiver in config['Host']['Receivers']:
			freq = receiver['Receiver']['freq']
			mode = receiver['Receiver']['mode']
			opt = receiver['Receiver']['additional_options']
			port = receiver['Receiver']['port']
			device_index = receiver['Receiver']['device_index']
			cmdline = f"rtl_fm -d {device_index} -f{freq} -M {mode} {opt} - |socat -u - TCP-LISTEN:{port}"
			th = threading.Thread(target=self.execute_rtl_fm, args=([client, device_index, port, user, cmdline]))
			th.start()
			rtl_fm_threds.append(th)

		print("\nall process started.")
		time.sleep(3)
		self.execute_sock2wav(config)

	def execute_sock2wav(self, config):
		sock2wave_path = config['sock2wav']['path']
		output_path = config['sock2wav']['output_path']
		ip_addr = config['Host']['ip_addr']

		lame_path = config['lame']['path']
		lame_opt = config['lame']['options']
		mp3_output_path = config['lame']['output_path']
		if mp3_output_path[-1] is not '/':
			mp3_output_path = mp3_output_path + "/"

		running_procs = []
		for rcv in config['Host']['Receivers']:

			# Filename rule
			# station_name: Tokyo Control West Sector
			# freq: 120.5M
			# ↓
			# TokyoControlWestSector##120@5M##__2020_08_01_11_30_20.wave
			receiver = rcv['Receiver']
			wav_file_name = receiver['station_name'].replace(" ", "") + "##" + receiver['freq'].replace(".", "@") + "##"
			arg = f" -i {ip_addr} -P {receiver['port']} -p {output_path} -s 32000 -S 1000 {wav_file_name}"
			cmdline = sock2wave_path + arg
			p = subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE,bufsize=2)

			# set stdout non blocking
			running_procs.append(p)
			#time.sleep(1)

		while running_procs:
			for proc in running_procs:
				retcode = proc.poll()
				if retcode is not None:
					running_procs.remove(proc)
					break

				# Detect .wav file write is complete.
				output = proc.stdout.readline().decode('utf-8')
				if 'file output:' in output:

					# convert wav to mp3
					output = output.replace('\n', '').replace('\r', '')
					wav_full_filename = output.split('file output:')[1]
					wavfilename = wav_full_filename.split(output_path.replace('~', ''))[1]
					mp3_fullfilename = mp3_output_path + wavfilename.replace(".wav", ".mp3")
					lame_cmd = lame_path + " " + lame_opt + " " + wav_full_filename + " " + mp3_fullfilename

					print("converting wav to mp3.")
					print(lame_cmd)
					subprocess.run(lame_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

					# delete .wav file
					print(".wav file delete.")
					os.remove(wav_full_filename)

	def execute_rtl_fm(self, client, device_index, port, user, cmdline):
		print(f"Launch rtl_fm via ssh : {cmdline}\n")
		stdin, stdout, stderr = client.exec_command(cmdline)
		for error_line in stderr:
			if "): Address already in use" in error_line:
				self.kill_others_process(client, device_index, port, user)
				time.sleep(3)
				return self.execute_rtl_fm(client, device_index, port, user, cmdline)
		return True

	def kill_others_process(self, client, device_index, port, user):
		# kill socat
		stdin, stdout, stderr = client.exec_command(f"lsof -i:{port}")
		linecount = 0
		for line in stdout:
			linecount+=1
			if linecount == 2:
				pid = line.split("socat ")[1].split(user)[0].replace(" ", "")
				killer = f"kill -9 {pid}"
				print(f"other process is using port {port}")
				print(f"kill other process socat PID({pid}) : {killer}")
				client.exec_command(killer)
				break

		# kill rtl_fm
		stdin, stdout, stderr = client.exec_command(f"ps aux")
		for line in stdout:
			if f"rtl_fm -d {device_index}" in line:
				old_process = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
				killer = f"kill -9 {old_process}"
				print(f"kill other process rtl_fm PID({old_process}) : {killer}")
				client.exec_command(killer)
				break

	def kill_all_rtl_fm_process(self, client):
		stdin, stdout, stderr = client.exec_command(f"ps aux")
		for line in stdout:
			if 'rtl_fm ' in line or 'socat ' in line:
				old_process = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
				killer = f"kill -9 {old_process}"
				print(f"kill old rtl_fm / socat process : {killer}")
				client.exec_command(killer)


if __name__ == '__main__':
	SDRRecorder()
