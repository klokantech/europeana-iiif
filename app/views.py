"""Module which defines views - actions for url passed requests"""

import sys
import os
import re
from urlparse import urlparse

from flask import request, render_template, abort, url_for, g
import simplejson as json
from flask import current_app as app

from iiif_manifest_factory import ManifestFactory
from ingest import ingestQueue
from models import Item, Batch, SubBatch
from exceptions import NoItemInDb, ErrorItemImport
from helper import prepareTileSources


item_url_regular = re.compile(r"""
	^/
	(?P<unique_id>([-_.:~a-zA-Z0-9]){1,32})
	/?
	(?P<order>\d*)
	""", re.VERBOSE)

id_regular = re.compile(r"""
	^([-_.:~a-zA-Z0-9]){1,32}$
	""", re.VERBOSE)

url_regular = re.compile(ur'(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?\xab\xbb\u201c\u201d\u2018\u2019]))')

#@app.before_request
def before_request():
	g.db = app.extensions['redis'].redis


#@app.route('/')
def index():
	return render_template('index.html')


#@app.route('/<unique_id>')
#@app.route('/<unique_id>/<order>')
def iFrame(unique_id, order=None):
	if order is not None:
		try:
			order = int(order)
			
			if order < 0:
				return 'Wrong item sequence', 404
		except:
			return 'Wrong item sequence', 404
	else:
		order = -1
	
	try:
		item = Item(unique_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
	
	if order >= len(item.url):
		return 'Wrong item sequence', 404
	
	tile_sources = []
	
	if order == -1:
		count = 0
		
		for url in item.url:
			tile_sources.append(prepareTileSources(item, url, count))
			count += 1
		
		order = 0
	else:
		url = item.url[order]
		tile_sources.append(prepareTileSources(item, url, order))
		
	return render_template('iframe_openseadragon_inline.html', item = item, tile_sources = tile_sources, order = order)


#@app.route('/<unique_id>/manifest.json')
def iiifMeta(unique_id):
	try:
		item = Item(unique_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
	
	fac = ManifestFactory()
	fac.set_base_metadata_uri(app.config['SERVER_NAME'])
	fac.set_base_metadata_dir(os.path.abspath(os.path.dirname(__file__)))
	fac.set_base_image_uri(app.config['IIIF_SERVER'])
	fac.set_iiif_image_info(2.0, 2)
	
	mf = fac.manifest(ident=url_for('iiifMeta', unique_id=unique_id, _external=True), label=item.title)
	mf.description = item.description
	mf.license = item.license
	
	mf.set_metadata({"label":"Author", "value":item.creator})
	mf.set_metadata({"label":"Source", "value":item.source})
	mf.set_metadata({"label":"Institution", "value":item.institution})
	mf.set_metadata({"label":"Institution link", "value":item.institution_link})
	
	seq = mf.sequence(ident='http://%s/sequence/s.json' % app.config['SERVER_NAME'], label='Item %s - sequence 1' % unique_id)

	count = 0
	
	for url in item.url:
		if item.image_meta[url].has_key('width'):
			width = item.image_meta[url]['width']
		else:
			width = 1

		if item.image_meta[url].has_key('height'):
			height = item.image_meta[url]['height']
		else:
			height = 1
	
		cvs = seq.canvas(ident='http://%s/canvas/c%s.json' % (app.config['SERVER_NAME'], count), label='Item %s - image %s' % (unique_id, count))
		cvs.set_hw(height, width)
	
		anno = cvs.annotation()

		if count > 0:
			img = anno.image(ident='/%s/%s/full/full/0/native.jpg' % (unique_id, count))
			img.add_service(ident='%s/%s/%s' % (app.config['IIIF_SERVER'], unique_id, count), context='http://iiif.io/api/image/2/context.json', profile='http://iiif.io/api/image/2/profiles/level2.json')
		else:
			img = anno.image(ident='/%s/full/full/0/native.jpg' % unique_id)
			img.add_service(ident='%s/%s' % (app.config['IIIF_SERVER'], unique_id), context='http://iiif.io/api/image/2/context.json', profile='http://iiif.io/api/image/2/profiles/level2.json')
		
		img.width = width
		img.height = height
		
		count += 1

	return json.JSONEncoder().encode(mf.toJSON(top=True)), 200, {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}


#@app.route('/oembed', methods=['GET'])
def oEmbed():
	url = request.args.get('url', None)
	
	if url is None:
		return 'No url parameter provided', 404
	
	format = request.args.get('format', None)
	
	if format is None:
		format = 'json'
	
	if format not in ('json', 'xml'):
		return 'The format parameter must be "json" or "xml" (or blank)', 501
	
	p_url = urlparse(url)
	
	if p_url.scheme != 'http':
		return 'the http scheme must be used', 404
	
	if p_url.netloc != app.config['SERVER_NAME']:
		return 'Only urls on the same server are allowed', 404
	
	test = item_url_regular.search(p_url.path)
		
	if test:
		unique_id = test.group('unique_id')
		order = test.group('order')
		
		if order == '':
			order = 0
		else:
			order = int(order)
	else:
		return 'Unsupported format of ID', 404

	try:
		item = Item(unique_id)
	except NoItemInDb as err:
		return err.message, 404
	except ErrorItemImport as err:
		return err.message, 500
	
	if item.lock is True:
		return 'The item is being ingested', 404
		
	if order >= len(item.url):
		return 'Wrong item sequence', 404

	maxwidth = request.args.get('maxwidth', None)
	maxheight = request.args.get('maxheight', None)

	if maxwidth is not None:
		maxwidth = int(maxwidth)
	
	if maxheight is not None:
		maxheight = int(maxheight)

	if item.image_meta[item.url[order]].has_key('width'):
		width = int(item.image_meta[item.url[order]]['width'])
	else:
		width = -1

	if item.image_meta[item.url[order]].has_key('height'):
		height = int(item.image_meta[item.url[order]]['height'])
	else:
		height = -1
	
	if width != -1 and height != -1:
		ratio = float(width) / float(height)
	else:
		ratio = 1

	if width != -1:
		if maxwidth is not None and maxwidth < width:
			outwidth = maxwidth
		else:
			outwidth = 'full'
	else:
		if maxwidth is not None:
			outwidth = maxwidth
		else:
			outwidth = 'full'
	
	if height != -1:
		if maxheight is not None and maxheight < height:
			outheight = maxheight
		else:
			outheight = 'full'
	else:
		if maxheight is not None:
			outheight = maxheight
		else:
			outheight = 'full'
	
	if outwidth == 'full' and outheight == 'full':
		size = 'full'
	elif outwidth == 'full':
		size = ',%s' % outheight
		width = float(outheight) * ratio
		height =  outheight
	elif outheight == 'full':
		size = '%s,' % outwidth
		width = outwidth
		height = float(outwidth) / ratio
	else:
		size = '!%s,%s' % (outwidth, outheight)

		if ratio > (float(outwidth) / float(outheight)):
			width = outwidth
			height = float(outwidth) / ratio
		else:
			width = float(outheight) * ratio
			height = outheight
	
	data = {}
	data[u'version'] = '1.0'
	data[u'type'] = 'photo'
	data[u'title'] = item.title
	
	if order > 0:
		data[u'url'] = '%s/%s/%s/full/%s/0/native.jpg' % (app.config['IIIF_SERVER'], unique_id, order, size)
	else:
		data[u'url'] = '%s/%s/full/%s/0/native.jpg' % (app.config['IIIF_SERVER'], unique_id, size)
		
	data[u'width'] = '%.0f' % width
	data[u'height'] = '%.0f' % height

	if format == 'xml':
		return render_template('oembed_xml.html', data = data), 200, {'Content-Type': 'text/xml'}
	else:
		return json.dumps(data), 200, {'Content-Type': 'application/json'}


#@app.route('/ingest', methods=['GET', 'POST'])
def ingest():
	if request.method == 'GET':
		batch_id = request.args.get('batch_id', None)

		if batch_id is not None:
		
			try:
				batch = Batch(batch_id)
			except:
				batch = None
	
			if batch:
				return json.JSONEncoder().encode(batch.items), 200, {'Content-Type': 'application/json'}
		
		abort(404)
	else:
		if request.headers.get('Content-Type') != 'application/json':
			abort(404)
		
		try:
			data = json.loads(request.data)
		except:
			abort(404)

		if type(data) is not list:
			abort(404)
		
		item_ids = []
		
		# validation
		for item in data:
			if type(item) is not dict:
				abort(404)

			if not item.has_key('id'):
				abort(404)
			
			if item['id'] in item_ids:
				abort(404)
					
			if not id_regular.match(item['id']):
				abort(404)
			
			if item.has_key('status') and (len(item) != 2 or item['status'] != 'deleted'):
				abort(404)
			
			if item.has_key('status'):
				continue
			
			if not item.has_key('url') or type(item['url']) != list or len(item['url']) == 0:
				abort(404)
			
			for url in item['url']:
				if not url_regular.match(url):
					abort(404)
			
			item_ids.append(item['id'])
		
		batch = Batch()
		sub_batches_count = 0
		
		# processing
		for item_data in data:
			unique_id = item_data['id']
			b = {'id': unique_id}
			num_url_ingested = 0
			
			if item_data.has_key('status') and item_data['status'] == 'deleted':
				g.db.delete(unique_id)
				b['status'] = 'deleted'
				batch.items.append(b)
				continue
			
			try:
				item = Item(unique_id, item_data)
				item.lock = True
			except NoItemInDb, ErrorItemImport:
				abort(500)
						
			try:
				old_item = Item(unique_id)
			except NoItemInDb, ErrorItemImport:
				old_item = None
					
			# already stored item
			if old_item:
				item.image_meta = old_item.image_meta
				order = 0

				for url in item.url:
					# any change in url
					if url not in old_item.url:
						item.image_meta[url] = {}
						sub_batches_count += 1
						data = {'url': url, 'item_id': item.id, 'order': order}
						sub_batch = SubBatch(sub_batches_count, batch.id, data)
						batch.sub_batches_ids.append(sub_batch.id)
						num_url_ingested += 1
					
					order += 1
						
			# new item
			else:
				item.image_meta = {}
				order = 0
				
				for url in item.url:
					sub_batches_count += 1
					data = {'url': url, 'item_id': item.id, 'order': order}
					sub_batch = SubBatch(sub_batches_count, batch.id, data)
					batch.sub_batches_ids.append(sub_batch.id)
					num_url_ingested += 1
					order += 1
			
			if num_url_ingested > 0:
				b['status'] = 'pending'
			else:
				b['status'] = 'ok'
				item.lock = False
			
			item.save()
			
			batch.items.append(b)

		batch.sub_batches_count = sub_batches_count
		batch.save()
		
		for sub_batch_id in batch.sub_batches_ids:
			ingestQueue.delay(batch.id, sub_batch_id)
		
	return json.JSONEncoder().encode({'batch_id': batch.id}), 200, {'Content-Type': 'application/json'}
