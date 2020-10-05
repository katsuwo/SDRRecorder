import yaml
import sys
import paramiko
import re
import threading
import time
import subprocess
import os
import boto3
import datetime

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

        receiver_is_local = True
        if self.config['ReceiverHost']['ip_addr'] != "127.0.0.1":
            receiver_is_local = False

        if receiver_is_local:
            # kill old rtl_tcp on localhost
            self.kill_process("rtl_tcp")
        else:
            # setup Paramiko SSH client
            self.client = paramiko.SSHClient()
            self.setup_ssh_client(self.config, self.client)

            # kill old rtl_tcp via ssh
            self.kill_all_rtl_tcp_process_via_ssh(self.client, process_string="rtl_tcp")

        # setup s3 Client
        try:
            self.s3 = self.setup_s3_client(config=self.config)
            resp = self.s3.list_buckets()
            if resp['ResponseMetadata']['HTTPStatusCode'] != 200:
                self.s3 = None
            else:
                found = False
                for b in resp['Buckets']:
                    if b['Name'] == self.config['S3_STORAGE']['S3_bucket_name']:
                        found = True
                        break
                if not found:
                    self.s3.create_bucket(Bucket=self.config['S3_STORAGE']['S3_bucket_name'])
        except ValueError as e:
            print(e)
            self.s3 = None

        # kill old socat
        self.kill_process(process_string="socat ")

        # kill old GRC_Recorder
        rcvr_name = self.config['Recorder']['GRC_Recorder']['script_path'].split("/")[-1]
        self.kill_process(rcvr_name)

        # launch rtl_tcp
        if receiver_is_local:
            self.open_receivers(config=self.config, client=None)
        else:
            self.open_receivers(config=self.config, client=self.client)

        self.execute_socat(self.config)
        time.sleep(3)
        self.execute_GRC_Receivers(self.config)
        time.sleep(3)
        self.execute_sock2wav(self.config)

    @staticmethod
    def read_configuration_file(file_name):
        with open(file_name, 'r') as yml:
            config = yaml.safe_load(yml)
        return config

    @staticmethod
    def check_configuration(config):
        if 'ReceiverHost' not in config:
            return False

        if 'Recorder' not in config:
            return False

        if 'sock2wav' not in config['Recorder']:
            return False

        sock2wav = config['Recorder']['sock2wav']
        if 'path' not in sock2wav:
            return False

        host = config['ReceiverHost']
        if 'ip_addr' not in host or 'Receivers' not in host:
            return False

        if not host['ip_addr']:
            return False

        receivers = host['Receivers']
        for receiver in receivers:
            if 'Receiver' not in receiver:
                return False
            rcv = receiver['Receiver']
            if 'device_index' not in rcv:
                return False
            if 'rtl_tcp_port' not in rcv:
                return False
            if 'grc_out_port' not in rcv:
                return False
            if 'socat_out_port' not in rcv:
                return False
            if 'station_name' not in rcv:
                return False
            if 'freq' not in rcv:
                return False
            if 'mode' not in rcv:
                return False
            if 'additional_options' not in rcv:
                return False
        return True

    def setup_ssh_client(self, config, client):
        host = config['ReceiverHost']['ip_addr']
        user = config['ReceiverHost']['user']
        password = config['ReceiverHost']['password']
        known_hosts = config['Recorder']['hostkey']['known_hosts_file']
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.load_host_keys(filename=known_hosts)
        client.connect(host, username=user, password=password)

    def setup_s3_client(self, config):
        if 'S3_STORAGE' not in config:
            raise ValueError("S3_STORAGE Section not exists in config file.")
        if 'S3_access_key_id' not in config['S3_STORAGE']:
            raise ValueError("S3_access_key_id element not exists in S3_STORAGE section.")
        if 'S3_secret_access_key' not in config['S3_STORAGE']:
            raise ValueError("S3_secret_access_key element not exists in S3_STORAGE section.")

        s3_endpoint_erl = self.config['S3_STORAGE']['S3_endpoint_url']
        os.environ['AWS_ACCESS_KEY_ID'] = self.config['S3_STORAGE']['S3_access_key_id']
        os.environ['AWS_SECRET_ACCESS_KEY'] = self.config['S3_STORAGE']['S3_secret_access_key']
        return boto3.client('s3', endpoint_url=s3_endpoint_erl, verify=False)

    def open_receivers(self, config, client=None):
        for receiver in config['ReceiverHost']['Receivers']:
            rtl_tcp_port = receiver['Receiver']['rtl_tcp_port']
            device_index = receiver['Receiver']['device_index']
            cmdline = f"rtl_tcp -a 0.0.0.0 -p {rtl_tcp_port} -d {device_index}"

            if client:
                self.execute_rtl_tcp(client, cmdline, local=False)
            else:
                self.execute_rtl_tcp(client, cmdline, local=True)

        print(f"Started {len(config['ReceiverHost']['Receivers'])} rtl_tcp processes.")
        print("-------------------------")

    def execute_socat(self, config):
        print("Run socat.")
        for rcv in config['ReceiverHost']['Receivers']:
            src_port = rcv['Receiver']['grc_out_port']
            dest_port = rcv['Receiver']['socat_out_port']
            cmdline = f"socat udp-listen:{src_port} tcp-listen:{dest_port} &"
            print(cmdline)
            subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Done.")

    def execute_GRC_Receivers(self, config):
        pre_execute_cmd = config['Recorder']['GRC_Recorder']['pre_execute_cmd']
        if pre_execute_cmd is not None:
            pre_execute_cmd = pre_execute_cmd + ";"
        else:
            pre_execute_cmd = ""

        host = config['ReceiverHost']['ip_addr']
        for rc in config['ReceiverHost']['Receivers']:
            rcvr = rc['Receiver']
            in_port = rcvr['rtl_tcp_port']
            freq = rcvr['freq']
            gain = rcvr['gain']
            sql = rcvr['squelch']
            correction = rcvr['freq_correct']

            dest_host = "127.0.0.1"
            dest_port = rcvr['grc_out_port']

            python_path = config['Recorder']['GRC_Recorder']['python27_path']
            script_path = config['Recorder']['GRC_Recorder']['script_path']

            arg = f"-f {freq} -g {gain} -s {sql} -c {correction} -H {host} -P {in_port} -d {dest_host} -p {dest_port}"
            cmdline = f"{pre_execute_cmd}{python_path} {script_path} {arg}"
            subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, bufsize=2)

    def execute_sock2wav(self, config):
        sock2wave_path = config['Recorder']['sock2wav']['path']
        output_path = config['Recorder']['sock2wav']['output_path']
        ip_addr = config['ReceiverHost']['ip_addr']

        lame_path = config['Recorder']['lame']['path']
        lame_opt = config['Recorder']['lame']['options']
        mp3_output_path = config['Recorder']['lame']['output_path']
        if mp3_output_path[-1] != '/':
            mp3_output_path = mp3_output_path + "/"

        running_procs = []
        for rcv in config['ReceiverHost']['Receivers']:
            # Filename rule
            # station_name: Tokyo Control West Sector
            # freq: 120.5M
            # ↓
            # TokyoControlWestSector_120_5M__2020_08_01_11_30_20.wave
            receiver = rcv['Receiver']
            wav_file_name = receiver['freq'].replace(".", "_") + "_" + receiver['station_name'].replace(" ", "") + "__"
            socat_out_port = receiver['socat_out_port']
            split_time = config['Recorder']['sock2wav']['file_split_Time']
            arg = f" -i 127.0.0.1 -P {socat_out_port} -p {output_path} -s 48000 -T {split_time} {wav_file_name}"
            cmdline = sock2wave_path + arg
            print(cmdline)
            p = subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=2)

            # set stdout non blocking
            running_procs.append(p)
        #			time.sleep(1)

        while running_procs:
            for proc in running_procs:
                retcode = proc.poll()
                if retcode is not None:
                    running_procs.remove(proc)
                    break

                # Detect .wav file write complete.
                output = proc.stdout.readline().decode('utf-8')
                if 'file output:' in output:
                    # convert wav to mp3
                    output = output.replace('\n', '').replace('\r', '')
                    wav_full_filename = output.split('file output:')[1]
                    wavfilename = wav_full_filename.split(output_path.replace('~', ''))[1].replace('/', '')
                    mp3filename = wavfilename.replace(".wav", ".mp3")
                    mp3_fullfilename = mp3_output_path + mp3filename
                    lame_cmd = lame_path + " " + lame_opt + " " + wav_full_filename + " /" + mp3_fullfilename

                    print("converting wav to mp3.")
                    print(lame_cmd)
                    ret = subprocess.run(lame_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if ret.returncode == 0:
                        print("convert success")
                        if self.s3 is not None:
                            print("upload to s3 storage")
                            today = str(datetime.date.today())
                            self.s3.upload_file(mp3_fullfilename,
                                                self.config['S3_STORAGE']['S3_BUCKET_NAME'],
                                                today + '/' + receiver['freq'] + "/" + mp3filename)
                        print(".mp3 file delete.")
                        os.remove(mp3_fullfilename)

                    # delete .wav file
                    print(".wav file delete.")
                    os.remove(wav_full_filename)

    def execute_rtl_tcp(self, client, cmdline, local=False):
        print(f"Launch rtl_tcp : {cmdline}")
        if local:
            subp = subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            print(f"Launch rtl_tcp via ssh : {cmdline}")
            stdin, stdout, stderr = client.exec_command(cmdline)

    def kill_others_process(self, client, device_index, port, user):
        # kill socat
        stdin, stdout, stderr = client.exec_command(f"lsof -i:{port}")
        linecount = 0
        for line in stdout:
            linecount += 1
            if linecount == 2:
                pid = line.split("socat ")[1].split(user)[0].replace(" ", "")
                killer = f"kill -9 {pid}"
                print(f"other process is using port {port}")
                print(f"kill other process socat PID({pid}) : {killer}")
                client.exec_command(killer)
                break

        # kill rtl_tcp
        stdin, stdout, stderr = client.exec_command(f"ps aux")
        for line in stdout:
            if f"rtl_tcp -a {device_index}" in line:
                old_process = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
                killer = f"kill -9 {old_process}"
                print(f"kill other process rtl_fm PID({old_process}) : {killer}")
                client.exec_command(killer)
                break
        print("-------------------------")

    def kill_all_rtl_tcp_process_via_ssh(self, client, process_string):
        old_procs = []
        stdin, stdout, stderr = client.exec_command(f"ps aux")
        for line in stdout:
            if process_string in line and "/bin/sh -c ps aux" not in line and "grep" not in line:
                p = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
                old_procs.append(p)

        for p in old_procs:
            killer = f"kill -9 {p}"
            print(f"kill old rtl_tcp : {killer}")
            client.exec_command(killer)
        print("-------------------------")

    def kill_process(self, process_string):
        cmdline = f"ps aux | grep {process_string}"
        print(f"kill {process_string} process.")
        old_procs = []
        subp = subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        while True:
            line = subp.stdout.readline().decode('utf-8')
            if process_string in line and "/bin/sh -c ps aux" not in line and "grep" not in line:
                print(line)
                p = re.sub(r'^[a-zA-Z0-9]+\s+', '', line).split(" ")[0]
                old_procs.append(p)
            if not line and subp.poll() is not None:
                break

        if len(old_procs) == 0:
            print("not found.")
        for p in old_procs:
            ret = subprocess.run(["kill", '-9', p])
            if ret == 0:
                print(f"PID:{p} was killed.")
            else:
                print(f"Failed kill PID:{p}")
        print("-------------------------")


if __name__ == '__main__':
    SDRRecorder()
