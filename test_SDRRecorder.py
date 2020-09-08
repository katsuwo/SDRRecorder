import unittest
import SDRRecorder

class TestSDRServer(unittest.TestCase):

	def test_read_configuration(self):
		sdrrecorder = SDRRecorder.SDRRecorder()

		with self.assertRaises(FileNotFoundError):
			sdrrecorder.read_configuration_file("./not_exists.yaml")
		sdrrecorder.read_configuration_file("./config.yaml")

	def test_check_configuration(self):
		sdrrecorder = SDRRecorder.SDRRecorder()
		config = sdrrecorder.read_configuration_file("./config.yaml")
		if not sdrrecorder.check_configuration(config):
			self.fail()

	def test_open_receivers(self):
		self.fail()


if __name__ == '__main__':
	unittest.main()
