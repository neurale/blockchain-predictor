import sys
import os
from datetime import timezone, timedelta, datetime as dt
import time
import dateutil.parser
import argparse
import pickle
import io
import codecs

import pandas as pd
from arctic.date import CLOSED_OPEN
import numpy as np

sys.path.insert(0, os.path.realpath('dataset_models'))
from dataset_model import DatasetModel

from matrix_model import MatrixModel
from stacked_model import StackedModel
import database_tools as db



chunkStore = db.getChunkstore()

models = [MatrixModel(), StackedModel()]

save = True
debug = False

labelKey = 'closePrice'

def generateDataset(modelName, propertyNames, labelsType, start=None, end=None):
	print("Generating dataset for properties ", propertyNames, "and using model", modelName, "for range", start, end)

	model = None

	#get the model instance
	for mod in models:
		if mod.name == modelName:
			model = mod

	if model is None:
		print("Error: Couldn't find model ", modelName)
		return

	properties = []

	#make sure we don't go off bounds for any property
	start, end = db.getMasterInterval(chunkStore, propertyNames, start, end)

	#load the needed properties
	for prop in propertyNames:
		data = db.loadData(chunkStore, prop, start, end, True, CLOSED_OPEN)

		if type(data.iloc[0][prop]) == str: #if the property values have been encoded, decode them
			print("Running numpy array Arctic workaround for prop %s..." % prop)
			data[prop] = data[prop].apply(lambda x: pickle.loads(codecs.decode(x.encode(), "base64")))
		properties.append(data)

	for prop in properties:
		if len(properties[0]) != len(prop):
			print("Error: Length mismatch in the data properties.")
			return

	#feed the model the properties and let it generate
	dataset, dates, nextPrices =  model.generate(properties)

	labels, dates = generateLabels(dates, nextPrices, db.loadData(chunkStore, labelKey, start, None, True), labelsType)

	if len(dataset) != len(labels): #if we have a length mismatch, probably due to insufficient data for the last label
		print("Mismatch in lengths of dataset and labels, removing excessive entries")
		dataset = dataset[:len(labels)] #remove dataframes for which we have no labels

	return (dataset, labels, dates)

def generateLabels(dates, nextPrices, ticks, labelsType):
	"""Generates dataset labels for each passed date, getting data from ticks. dates MUST BE CHRONOLOGICALLY ORDERED. """
	if labelsType == "boolean":
		labels = []
		i=0
		
		indices = ticks.index.values

		for date in dates:
			while ticks.get_value(indices[i], 'date') != date:
				i+=1

			try:
				currPrice = ticks.get_value(indices[i], 'closePrice')
				nextPrice = ticks.get_value(indices[i+1], 'closePrice')
			except (ValueError, IndexError, KeyError):
				print("Failed to load the date after", date, ". Probably end of data. Will remove one dataset entry.")
				dates = dates[:len(labels)] #keep only the labeled dates
				break
			if debug:
				print(ticks.loc[indices[i] : indices[i+1]])

			label = nextPrice > currPrice
			
			if debug:
				print("Label for dataframe at %s is %s for prices curr/next : %s and %s" % (date, label, currPrice, nextPrice) )
			labels.append([label])

		#make numpy array
		labels = np.array(labels)
		
		return (labels, dates)

	elif labelsType == 'full': #nothing to do, the prices are already given and are normalized
		return (nextPrices, dates)

def randomizeDataset(dataset, labels, dates):
	permutation = np.random.permutation(labels.shape[0])
	shuffled_dataset = dataset[permutation,:,:]
	shuffled_labels = labels[permutation]
	shuffled_dates = dates[permutation]
	return shuffled_dataset, shuffled_labels, shuffled_dates

def saveDataset(filename, data):
	if save:
		#save the dataset to a file
		try:
			with open(filename, 'wb') as f:
				pickle.dump(data, f, pickle.HIGHEST_PROTOCOL)
		except Exception as e:
			print('Unable to save data to', filename, ':', e)

def run(model, properties, start, end, filename, labels, ratio, shuffle):
	start = dateutil.parser.parse(start) if start is not None else None
	end = dateutil.parser.parse(end) if end is not None else None

	try:
		ratio = [int(x) for x in ratio.split(':')]
	except ValueError:
		print("Error while reading the given ratio. Did you format it in the correct way?")
		return

	#generate the dataset
	dataset, labels, dates = generateDataset(model, properties.split(','), labels, start, end)

	print("Generated dataset and labels with length %s." % labels.shape[0])

	if shuffle:
		#randomize it
		dataset, labels, dates = randomizeDataset(dataset, labels, dates)
		print("Randomized dataset and labels.")

	if len(ratio) == 1:
		data = {
			'dataset': dataset,
			'labels': labels,
			'dates': dates
		}
	else:
		data = []

		split = [] #the lenghts of the dataset pieces
		for rat in ratio:
			split.append( int((rat * len(dataset)) / np.sum(ratio)) ) #calculate the length by keeping the given ratio

		print(split, ratio)

		index = 0

		for i, spl in enumerate(split):
			end = (spl + index) if i != len(split) -1 else None #because of integer division, add anything left on the last iteration

			data.append({
				'dataset': dataset[index:end],
				'labels': labels[index:end],
				'dates': dates[index:end]
			})
			index += spl

	#save it
	if save:
		saveDataset(filename, data)
		print("saved dataset and labels as %a." % filename)

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Generates a dataset by compiling generated data properties using a certain dataset model")
	parser.add_argument('--model', type=str, default='matrix', help='The name of the dataset model to use. Defaults to matrix.')
	parser.add_argument('properties', type=str, default='openPrice,closePrice,gasPrice', help='A list of the names of the properties to use, separated by a comma.')
	parser.add_argument('--start', type=str, default=None, help='The start date. YYYY-MM-DD-HH')
	parser.add_argument('--end', type=str, default=None, help='The end date. YYYY-MM-DD-HH')
	parser.add_argument('--filename', type=str, default=None, help='The target filename / dir to save the pickled dataset to. Defaults to "data/dataset.pickle"')
	parser.add_argument('--labels', type=str, default='full', choices=['boolean', 'full'], help='What kind of labels should be generated for each dataframe. "boolean" contains only the sign of the course, "full" consists of all other target predictions.')
	parser.add_argument('--ratio', type=str, default='1', help='On how many fragments to split the main dataset. For example, "1:2:3" will create three datasets with sizes proportional to what given.')
	parser.add_argument('--shuffle', dest='shuffle', action="store_true", help="Shuffle the generated dataset and labels.")
	parser.set_defaults(shuffle=False)

	args, _ = parser.parse_known_args()

	if args.filename == None:
		filename = "data/dataset_" + str(args.start) + "-" + str(args.end) + ".pickle"
	else: filename = args.filename

	run(args.model, args.properties, args.start, args.end, filename, args.labels, args.ratio, args.shuffle)
