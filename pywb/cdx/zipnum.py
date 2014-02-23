import os
import collections
import itertools
import logging
from cStringIO import StringIO
import datetime

from cdxsource import CDXSource
from cdxobject import IDXObject

from pywb.utils.loaders import BlockLoader
from pywb.utils.loaders import SeekableTextFileReader
from pywb.utils.bufferedreaders import gzip_decompressor
from pywb.utils.binsearch import iter_range, linearsearch


#=================================================================
class ZipBlocks:
    def __init__(self, part, offset, length, count):
        self.part = part
        self.offset = offset
        self.length = length
        self.count = count


#=================================================================
def readline_to_iter(stream):
    try:
        count = 0
        buff = stream.readline()
        while buff:
            count += 1
            yield buff
            buff = stream.readline()

    finally:
        stream.close()


#=================================================================
class ZipNumCluster(CDXSource):
    DEFAULT_RELOAD_INTERVAL = 10  # in minutes
    DEFAULT_MAX_BLOCKS = 50

    def __init__(self, summary, config=None):

        loc = None
        cookie_maker = None
        self.max_blocks = self.DEFAULT_MAX_BLOCKS
        reload_ival = self.DEFAULT_RELOAD_INTERVAL

        if config:
            loc = config.get('zipnum_loc')
            cookie_maker = config.get('cookie_maker')

            self.max_blocks = config.get('max_blocks',
                                         self.max_blocks)

            reload_ival = config.get('reload_interval', reload_ival)

        if not loc:
            splits = os.path.splitext(summary)
            loc = splits[0] + '.loc'

        self.summary = summary
        self.loc_filename = loc

        # initial loc map
        self.loc_map = {}
        self.load_loc()

        # reload interval
        self.loc_update_time = datetime.datetime.now()
        self.reload_interval = datetime.timedelta(minutes=reload_ival)

        self.blk_loader = BlockLoader(cookie_maker=cookie_maker)

    def load_loc(self):
        logging.debug('Loading loc from: ' + self.loc_filename)
        with open(self.loc_filename) as fh:
            for line in fh:
                parts = line.rstrip().split('\t')
                self.loc_map[parts[0]] = parts[1:]

    @staticmethod
    def reload_timed(timestamp, val, delta, func):
        now = datetime.datetime.now()
        if now - timestamp >= delta:
            func()
            return now
        return None

    def reload_loc(self):
        newtime = self.reload_timed(self.loc_update_time,
                                    self.loc_map,
                                    self.reload_interval,
                                    self.load_loc)

        if newtime:
            self.loc_update_time = newtime

    def lookup_loc(self, part):
        return self.loc_map[part]

    def load_cdx(self, params):
        self.reload_loc()

        reader = SeekableTextFileReader(self.summary)

        idx_iter = iter_range(reader,
                              params['key'],
                              params['end_key'],
                              prev_size=1)

        if params.get('showPagedIndex'):
            params['proxyAll'] = True
            return idx_iter
        else:
            blocks = self.idx_to_cdx(idx_iter, params)

            def gen_cdx():
                for blk in blocks:
                    for cdx in blk:
                        yield cdx

            return gen_cdx()

    def idx_to_cdx(self, idx_iter, params):
        blocks = None
        ranges = []

        for idx in idx_iter:
            idx = IDXObject(idx)

            if (blocks and blocks.part == idx['part'] and
                blocks.offset + blocks.length == idx['offset'] and
                blocks.count < self.max_blocks):

                    blocks.length += idx['length']
                    blocks.count += 1
                    ranges.append(idx['length'])

            else:
                if blocks:
                    yield self.block_to_cdx_iter(blocks, ranges, params)

                blocks = ZipBlocks(idx['part'],
                                   idx['offset'],
                                   idx['length'],
                                   1)

                ranges = [blocks.length]

        if blocks:
            yield self.block_to_cdx_iter(blocks, ranges, params)

    def block_to_cdx_iter(self, blocks, ranges, params):
        last_exc = None
        last_traceback = None

        for location in self.lookup_loc(blocks.part):
            try:
                return self.load_blocks(location, blocks, ranges, params)
            except Exception as exc:
                last_exc = exc
                import sys
                last_traceback = sys.exc_info()[2]

        if last_exc:
            raise exc, None, last_traceback
        else:
            raise Exception('No Locations Found for: ' + block.part)

    def load_blocks(self, location, blocks, ranges, params):

        if (logging.getLogger().getEffectiveLevel() <= logging.DEBUG):
            msg = 'Loading {b.count} blocks from {loc}:{b.offset}+{b.length}'
            logging.debug(msg.format(b=blocks, loc=location))

        reader = self.blk_loader.load(location, blocks.offset, blocks.length)

        def decompress_block(range_):
            decomp = gzip_decompressor()
            buff = decomp.decompress(reader.read(range_))
            return readline_to_iter(StringIO(buff))

        iter_ = itertools.chain(*itertools.imap(decompress_block, ranges))

        # start bound
        iter_ = linearsearch(iter_, params['key'])

        # end bound
        end = params['end_key']
        iter_ = itertools.takewhile(lambda line: line < end, iter_)
        return iter_