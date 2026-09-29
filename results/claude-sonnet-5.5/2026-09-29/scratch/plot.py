import json,sys
import numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
d=json.load(open(sys.argv[1])); n=d['n']; cx=d['cx']
fr=list(d['frames'].keys()); fig,ax=plt.subplots(2,len(fr)//2,figsize=(20,7))
for a,k in zip(ax.flat,fr):
    P=np.array(d['frames'][k]).reshape(n,n,3)
    for j in range(0,n,4): a.plot(P[j,:,0],P[j,:,2],'k-',lw=.3)
    for i in range(0,n,4): a.plot(P[:,i,0],P[:,i,2],'r-',lw=.3)
    a.set_title('frame '+k+' x-z'); a.set_aspect('equal')
plt.savefig(sys.argv[2],dpi=70)
