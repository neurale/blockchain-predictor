from property import Property

class PropertyWeightedAverage(Property):
	def __init__(self):
		self.name = "weightedAverage"
		self.requires = ['tick']

	def processTick(self, data):
		return self.averageOfColumn(data[self.requires[0]], 'weightedAverage')
