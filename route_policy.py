"""Measured route selection with hysteresis; no assumptions about WAN speed."""
from dataclasses import dataclass
import math
import time

SAMPLE_LIFETIME_SECONDS=60.
SWITCH_HOLD_SECONDS=5.
SWITCH_GAIN=1.25
REQUIRED_WINS=2
ROUTE_ORDER={'lan':0,'p2p':1,'relay':2,'cloud':3}


@dataclass
class Measurement:
    rate:float
    when:float


class RoutePolicy:
    def __init__(self,clock=time.perf_counter):
        self.clock=clock;self.samples={};self.current=None
        self.changed=0.;self.challenger=None;self.wins=0
        self.revision=0;self.decision_revision=0

    def observe(self,key,byte_count,elapsed):
        if byte_count<=0 or not math.isfinite(elapsed) or elapsed<=0:return
        now=self.clock();rate=byte_count/elapsed;old=self.samples.get(key)
        if old and now-old.when<SAMPLE_LIFETIME_SECONDS:rate=.5*old.rate+.5*rate
        self.samples[key]=Measurement(rate,now)
        self.revision+=1

    def choose(self,routes):
        """routes maps live connection identities to their actual transport."""
        now=self.clock()
        if self.current not in routes:
            self.current=min(routes,key=lambda key:(ROUTE_ORDER[routes[key]],key),default=None)
            self.changed=now;self.challenger=None;self.wins=0
            return self.current
        fresh={key:sample.rate for key,sample in self.samples.items()
               if key in routes and now-sample.when<SAMPLE_LIFETIME_SECONDS}
        if self.current not in fresh:
            preferred=min(routes,key=lambda key:(ROUTE_ORDER[routes[key]],key))
            if preferred!=self.current:
                self.current=preferred;self.changed=now;self.challenger=None;self.wins=0
            return self.current
        best=max(fresh,key=fresh.get)
        if best==self.current or fresh[best]<fresh[self.current]*SWITCH_GAIN:
            self.challenger=None;self.wins=0;return self.current
        if self.challenger!=best:self.challenger=best;self.wins=0
        if self.decision_revision!=self.revision:
            self.wins+=1;self.decision_revision=self.revision
        if self.wins>=REQUIRED_WINS and now-self.changed>=SWITCH_HOLD_SECONDS:
            self.current=best;self.changed=now;self.challenger=None;self.wins=0
        return self.current
