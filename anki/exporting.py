# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import itertools, time, re, os, HTMLParser
from operator import itemgetter
#from anki import Deck
from anki.cards import Card
from anki.sync import SyncClient, SyncServer, copyLocalMedia
from anki.lang import _
from anki.utils import parseTags, stripHTML, ids2str

class Exporter(object):
    def __init__(self, col):
        self.col = col
        self.limitTags = []
        self.limitCardIds = []

    def exportInto(self, path):
        self._escapeCount = 0
        file = open(path, "wb")
        self.doExport(file)
        file.close()

    def escapeText(self, text, removeFields=False):
        "Escape newlines and tabs, and strip Anki HTML."
        from BeautifulSoup import BeautifulSoup as BS
        text = text.replace("\n", "<br>")
        text = text.replace("\t", " " * 8)
        if removeFields:
            # beautifulsoup is slow
            self._escapeCount += 1
            try:
                s = BS(text)
                all = s('span', {'class': re.compile("fm.*")})
                for e in all:
                    e.replaceWith("".join([unicode(x) for x in e.contents]))
                text = unicode(s)
            except HTMLParser.HTMLParseError:
                pass
        return text

    def cardIds(self):
        "Return all cards, limited by tags or provided ids."
        if self.limitCardIds:
            return self.limitCardIds
        if not self.limitTags:
            cards = self.col.db.column0("select id from cards")
        else:
            d = tagIds(self.col.db, self.limitTags, create=False)
            cards = self.col.db.column0(
                "select cardId from cardTags where tagid in %s" %
                ids2str(d.values()))
        self.count = len(cards)
        return cards

class AnkiExporter(Exporter):

    key = _("Anki Collection (*.anki)")
    ext = ".anki"

    def __init__(self, col):
        Exporter.__init__(self, col)
        self.includeSchedulingInfo = False
        self.includeMedia = True

    def exportInto(self, path):
        n = 3
        if not self.includeSchedulingInfo:
            n += 1
        try:
            os.unlink(path)
        except (IOError, OSError):
            pass
        self.newCol = DeckStorage.Deck(path)
        client = SyncClient(self.deck)
        server = SyncServer(self.newDeck)
        client.setServer(server)
        client.localTime = self.deck.modified
        client.remoteTime = 0
        self.deck.db.flush()
        # set up a custom change list and sync
        lsum = self.localSummary()
        rsum = server.summary(0)
        payload = client.genPayload((lsum, rsum))
        res = server.applyPayload(payload)
        if not self.includeSchedulingInfo:
            self.newDeck.resetCards()
        # media
        if self.includeMedia:
            server.deck.mediaPrefix = ""
            copyLocalMedia(client.deck, server.deck)
        # need to save manually
        self.newDeck.rebuildCounts()
        # FIXME
        #self.exportedCards = self.newDeck.cardCount
        self.newDeck.crt = 0
        self.newDeck.db.commit()
        self.newDeck.close()

    def localSummary(self):
        cardIds = self.cardIds()
        cStrIds = ids2str(cardIds)
        cards = self.deck.db.all("""
select id, modified from cards
where id in %s""" % cStrIds)
        notes = self.deck.db.all("""
select notes.id, notes.modified from cards, notes where
notes.id = cards.noteId and
cards.id in %s""" % cStrIds)
        models = self.deck.db.all("""
select models.id, models.modified from models, notes where
notes.modelId = models.id and
notes.id in %s""" % ids2str([f[0] for f in notes]))
        media = self.deck.db.all("""
select id, modified from media""")
        return {
            # cards
            "cards": cards,
            "delcards": [],
            # notes
            "notes": notes,
            "delnotes": [],
            # models
            "models": models,
            "delmodels": [],
            # media
            "media": media,
            "delmedia": [],
            }

class TextCardExporter(Exporter):

    key = _("Text files (*.txt)")
    ext = ".txt"

    def __init__(self, deck):
        Exporter.__init__(self, deck)
        self.includeTags = False

    def doExport(self, file):
        ids = self.cardIds()
        strids = ids2str(ids)
        cards = self.deck.db.all("""
select cards.question, cards.answer, cards.id from cards
where cards.id in %s
order by cards.created""" % strids)
        if self.includeTags:
            self.cardTags = dict(self.deck.db.all("""
select cards.id, notes.tags from cards, notes
where cards.noteId = notes.id
and cards.id in %s
order by cards.created""" % strids))
        out = u"\n".join(["%s\t%s%s" % (
            self.escapeText(c[0], removeFields=True),
            self.escapeText(c[1], removeFields=True),
            self.tags(c[2]))
                          for c in cards])
        if out:
            out += "\n"
        file.write(out.encode("utf-8"))
        self.deck.finishProgress()

    def tags(self, id):
        if self.includeTags:
            return "\t" + ", ".join(parseTags(self.cardTags[id]))
        return ""

class TextNoteExporter(Exporter):

    key = _("Text files (*.txt)")
    ext = ".txt"

    def __init__(self, deck):
        Exporter.__init__(self, deck)
        self.includeTags = False

    def doExport(self, file):
        cardIds = self.cardIds()
        notes = self.deck.db.all("""
select noteId, value, notes.created from notes, fields
where
notes.id in
(select distinct noteId from cards
where cards.id in %s)
and notes.id = fields.noteId
order by noteId, ordinal""" % ids2str(cardIds))
        txt = ""
        if self.includeTags:
            self.noteTags = dict(self.deck.db.all(
                "select id, tags from notes where id in %s" %
                ids2str([note[0] for note in notes])))
        groups = itertools.groupby(notes, itemgetter(0))
        groups = [[x for x in y[1]] for y in groups]
        groups = [(group[0][2],
                   "\t".join([self.escapeText(x[1]) for x in group]) +
                   self.tags(group[0][0]))
                  for group in groups]
        groups.sort(key=itemgetter(0))
        out = [ret[1] for ret in groups]
        self.count = len(out)
        out = "\n".join(out)
        file.write(out.encode("utf-8"))
        self.deck.finishProgress()

    def tags(self, id):
        if self.includeTags:
            return "\t" + self.noteTags[id]
        return ""

# Export modules
##########################################################################

def exporters():
    return (
        (_("Anki Deck (*.anki)"), AnkiExporter),
        (_("Cards in tab-separated text file (*.txt)"), TextCardExporter),
        (_("Notes in tab-separated text file (*.txt)"), TextNoteExporter))
