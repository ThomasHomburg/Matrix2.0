import urllib.request as u
H={'User-Agent':'Tom Homburg t09homburg@gmail.com'}
for url in ['https://data.sec.gov/submissions/CIK0001067983.json','https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001067983&type=13F-HR&output=atom','https://www.sec.gov/Archives/edgar/data/1067983/']:
    try:
        print(u.urlopen(u.Request(url,headers=H),timeout=30).status, url)
    except Exception as e:
        print(e, url)
