import mpd
client=mpd.MPDClient()
client.connect("localhost", 6600)
client.update()
playlists = [x["playlist"] for x in client.listplaylists()]

client.clear()
playlistname='musicroot'
songnr=0
#print client.listall()

for x in client.listall():
	if 'file' in x.keys():
		client.add(x['file'])
		songnr=songnr+1
	if 'directory' in x.keys():
		if songnr>0:
			# save current playlist
			print "client.playlist", client.playlist()
			if playlistname in playlists: 
				client.rm(playlistname)
			client.save(playlistname)
	    # start new playlist
		playlistname=x['directory']
		client.clear()
		songnr=0
		print x['directory']

if songnr>0:
	if playlistname in playlists:
		client.rm(playlistname)
	client.save(playlistname)
