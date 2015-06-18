"""Module which defines views - actions for url passed requests"""

import sys
import os
import re
from urlparse import urlparse

from flask import request, render_template, abort, url_for, g
import simplejson as json
from flask import current_app as app
import bleach

from iiif_manifest_factory import ManifestFactory
from ingest import ingestQueue
from models import Item, Batch, SubBatch
from exceptions import NoItemInDb, ErrorItemImport
from helper import prepareTileSources, trimFileExtension, getCloudSearch


ALLOWED_TAGS = ['b', 'blockquote', 'code', 'em', 'i', 'li', 'ol', 'strong', 'ul']

item_url_regular = re.compile(r"""
	^/
	(?P<unique_id>([-_.:~a-zA-Z0-9]){1,255})
	/?
	(?P<order>\d*)
	""", re.VERBOSE)

id_regular = re.compile(r"""
	^([-_.:~a-zA-Z0-9]){1,255}$
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
		for url in item.url:
			tile_sources.append(prepareTileSources(item, url))
		
		order = 0
	else:
		url = item.url[order]
		tile_sources.append(prepareTileSources(item, url))
		
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

		img = anno.image(ident='/%s/full/full/0/native.jpg' % (trimFileExtension(item.image_meta[url]['filename'])))
		img.add_service(ident='%s/%s' % (app.config['IIIF_SERVER'], trimFileExtension(item.image_meta[url]['filename'])), context='http://iiif.io/api/image/2/context.json', profile='http://iiif.io/api/image/2/profiles/level2.json')
		
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
	data[u'url'] = '%s/%s/full/%s/0/native.jpg' % (app.config['IIIF_SERVER'], trimFileExtension(item.image_meta[item.url[order]]['filename']), size)
	data[u'width'] = '%.0f' % width
	data[u'height'] = '%.0f' % height
	data[u'author_name'] = item.creator
	data[u'author_url'] = item.source
	data[u'provider_name'] = item.institution
	data[u'provider_url'] = item.institution_link

	if format == 'xml':
		return render_template('oembed_xml.html', data = data), 200, {'Content-Type': 'text/xml'}
	else:
		return json.dumps(data), 200, {'Content-Type': 'application/json'}


#@app.route('/ingest', methods=['GET', 'POST'])
def ingest():
	# show info about a ingest
	if request.method == 'GET':
		batch_id = request.args.get('batch_id', None)

		if batch_id is not None:
		
			try:
				batch = Batch(batch_id)
			except:
				batch = None
	
			if batch:
				item_sub_batches = {}
				
				for sub_batch_id in batch.sub_batches_ids:
					sub_batch = SubBatch(sub_batch_id, batch.id)
					
					if not item_sub_batches.has_key('%s@%s' % (sub_batch.item_id, sub_batch.url)) or (item_sub_batches.has_key('%s@%s' % (sub_batch.item_id, sub_batch.url)) and item_sub_batches['%s@%s' % (sub_batch.item_id, sub_batch.url)] != 'ok'):
						item_sub_batches['%s@%s' % (sub_batch.item_id, sub_batch.url)] = sub_batch.status
				
				order = 0

				for i in batch.items:
					if i['status'] == 'deleted':
						continue
						
					i['urls'] = []
					
					for url in i['url']:
						# actualy ingested url
						if item_sub_batches.has_key('%s@%s' % (i['id'], url)):
							i['urls'].append(item_sub_batches['%s@%s' % (i['id'], url)])
						# ingested url in past
						else:
							i['urls'].append('ok')
					
					i.pop('url', None)
					
					batch.items[order] = i
					order += 1
					
				return json.JSONEncoder().encode(batch.items), 200, {'Content-Type': 'application/json'}
		
		abort(404)
	# new ingest
	else:
		if request.headers.get('Content-Type') != 'application/json':
			abort(404)
		
		try:
			data = json.loads(request.data)
		except:
			abort(404)

		if type(data) is not list or len(data) == 0:
			abort(404)
		
		item_ids = []
		errors = []
		
		# validation
		for order in range(0, len(data)):
			item = data[order]
			
			if type(item) is not dict:
				errors.append("The item num. %s must be inside of '{}'" % order)
				continue
			
			item = dict((k.lower(), v) for k, v in item.iteritems())

			if not item.has_key('id'):
				errors.append("The item num. %s must have unique ID" % order)
				continue
			
			if item['id'] in item_ids:
				errors.append("The item num. %s must have unique ID" % order)
				continue
					
			if not id_regular.match(item['id']):
				errors.append("The item num. %s must have valid ID" % order)
			
			if item.has_key('status') and (len(item) != 2 or item['status'] != 'deleted'):
				errors.append("The item num. %s has status, but it isn't set to 'deleted' or there are more fields" % order)
				continue
			
			if item.has_key('status'):
				continue
			
			# another tests are usefull only for items which aren't marked to be deleted
			
			# convert some input field's names to the internal names
			if item.has_key('institutionlink'):
				item['institution_link'] = item['institutionlink']
				item.pop('institutionlink', None)
			if item.has_key('imageurl'):
				item['url'] = item['imageurl']
				item.pop('imageurl', None)
			
			if not item.has_key('url') or type(item['url']) != list or len(item['url']) == 0:
				errors.append("The item num. %s doesn't have url field, or it isn't a list or a list is empty" % order)
				continue
			
			for url in item['url']:
				if not url_regular.match(url):
					errors.append("The '%s' url in the item num. %s isn't valid url" % (url, order))
			
			for key in item.keys():
				if key not in ['id', 'title', 'creator', 'source', 'institution', 'institution_link', 'license', 'description', 'url']:
					errors.append("The item num. %s has a not allowed field '%s'" % (order, key))
			
			if item.has_key('source') and item['source'] and not url_regular.match(item['source']):
				errors.append("The item num. %s doesn't have valid url '%s' in the Source field" % (order, item['source']))
			
			if item.has_key('institution_link') and item['institution_link'] and not url_regular.match(item['institution_link']):
				errors.append("The item num. %s doesn't have valid url '%s' in the InstitutionLink field" % (order, item['institution_link']))
			
			if item.has_key('license') and item['license'] and not url_regular.match(item['license']):
				errors.append("The item num. %s doesn't have valid url '%s' in the License field" % (order, item['license']))
			
			item_ids.append(item['id'])
			data[order] = item
		
		if errors:
			return json.dumps({'errors': errors}), 404, {'Content-Type': 'application/json'}
		
		batch = Batch()
		sub_batches_count = 0
		cloudsearch_direct_items = []
		
		# processing
		for item_data in data:
			unique_id = item_data['id']
			b = {'id': unique_id} # batch for one item
			item_url_ingested_count = 0
			
			# delete a item
			if item_data.has_key('status') and item_data['status'] == 'deleted':
				try:
					item = Item(unique_id)
					item.lock = True
					item.save()
					
					for url in item.url:
						data = {'url': url, 'item_id': item.id, 'type': 'del'}
						sub_batch = SubBatch(sub_batches_count, batch.id, data)
						batch.sub_batches_ids.append(sub_batch.id)
						sub_batches_count += 1
						
				except NoItemInDb, ErrorItemImport:
					pass
					
				b['status'] = 'deleted'
				
				batch.items.append(b)
				continue
			
			b['url'] = item_data['url']
			
			# update or create a new item
			try:
				# sanitising input
				if item_data.has_key('title'):
					item_data['title'] = bleach.clean(item_data['title'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('creator'):
					item_data['creator'] = bleach.clean(item_data['creator'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('institution'):
					item_data['institution'] = bleach.clean(item_data['institution'], tags=[], attributes=[], styles=[], strip=True)
				if item_data.has_key('description'):
					item_data['description'] = bleach.clean(item_data['description'], tags=ALLOWED_TAGS, attributes=[], styles=[], strip=True)

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
				new_count = len(item.url)
				old_count = len(old_item.url)
				
				for order in range(0, max(new_count, old_count)):
					if order < new_count and order < old_count:
						# different url on the specific position --> overwrite
						if item.url[order] != old_item.url[order]:
							data = {'url': item.url[order], 'item_id': item.id, 'order': order, 'type': 'add'}
							sub_batch = SubBatch(sub_batches_count, batch.id, data)
							batch.sub_batches_ids.append(sub_batch.id)
							item_url_ingested_count += 1
							sub_batches_count += 1
					else:
						# end of both lists
						if new_count == old_count:
							break
						
						# a new url list is shorter than old one --> something to delelete
						if order >= new_count:
							data = {'url': old_item.url[order], 'item_id': item.id, 'type': 'del'}
							sub_batch = SubBatch(sub_batches_count, batch.id, data)
							batch.sub_batches_ids.append(sub_batch.id)
							item_url_ingested_count += 1
							sub_batches_count += 1
						# a new url list is longer than old one --> something to add
						elif order >= old_count:
							data = {'url': item.url[order], 'item_id': item.id, 'order': order, 'type': 'add'}
							sub_batch = SubBatch(sub_batches_count, batch.id, data)
							batch.sub_batches_ids.append(sub_batch.id)
							item_url_ingested_count += 1
							sub_batches_count += 1
				
				# only metadata are changed not images --> no ingest --> update changes to CloudSearch directly
				if item_url_ingested_count == 0:
					cloudsearch_direct_items.append(item)
					
			# new item
			else:
				order = 0
				
				for url in item.url:
					data = {'url': url, 'item_id': item.id, 'order': order}
					sub_batch = SubBatch(sub_batches_count, batch.id, data)
					batch.sub_batches_ids.append(sub_batch.id)
					item_url_ingested_count += 1
					sub_batches_count += 1
					order += 1
			
			if item_url_ingested_count > 0:
				b['status'] = 'pending'
			else:
				b['status'] = 'ok'
				item.lock = False
			
			item.save()
			
			batch.items.append(b)

		batch.sub_batches_count = sub_batches_count
		batch.save()
		
		if cloudsearch_direct_items:
			try:
				cloudsearch = getCloudSearch()
				
				for item in cloudsearch_direct_items:
					cloudsearch.add(item.id, {'id': item.id, 'title': item.title, 'creator': item.creator, 'source': item.source, 'institution': item.institution, 'institution_link': item.institution_link, 'license': item.license, 'description': item.description})
				
				cloudsearch.commit()
				cloudsearch.clear_sdf()
			except:
				#TODO do some log
				pass
		
		for sub_batch_id in batch.sub_batches_ids:
			ingestQueue.delay(batch.id, sub_batch_id)
		
	return json.JSONEncoder().encode({'batch_id': batch.id}), 200, {'Content-Type': 'application/json'}