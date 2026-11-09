import sys, runpy, urllib.request as u
orig = u.urlopen
def w(req, *a, **k):
    print('REQ', getattr(req, 'full_url', req), dict(getattr(req, 'headers', {})))
    return orig(req, *a, **k)
u.urlopen = w
sys.argv = ['smart_money.py', 'fetch']
runpy.run_path('smart_money.py', run_name='__main__')
