"""A stand-in for pyPreservica's EntityAPI, holding sample MODS records in memory."""
import xml.etree.ElementTree as ET
from pyPreservica import EntityType

NS = "http://www.loc.gov/mods/v3"

A1 = f"""<mods xmlns="{NS}" xmlns:xlink="http://www.w3.org/1999/xlink" version="3.7">
  <titleInfo><title>Campus protest, Main Quad</title></titleInfo>
  <name type="personal"><namePart>Smith, Jon</namePart><role><roleTerm type="text" authority="marcrelator">photographer</roleTerm></role></name>
  <name type="corporate"><namePart>University News Bureau</namePart></name>
  <typeOfResource>still image</typeOfResource>
  <genre authority="aat">photographs</genre>
  <subject authority="lcsh"><topic valueURI="http://id.loc.gov/authorities/subjects/sh85129244">Student movments</topic></subject>
  <subject authority="lcsh"><topic valueURI="http://id.loc.gov/authorities/subjects/sh85034149">Demonstrations</topic></subject>
  <subject authority="lcsh"><topic valueURI="http://id.loc.gov/authorities/subjects/sh85026282">College students</topic></subject>
  <subject><geographic>Illinois</geographic></subject>
  <originInfo><dateCreated encoding="w3cdtf" keyDate="yes">1969-05-01</dateCreated></originInfo>
  <language><languageTerm type="code" authority="iso639-2b">eng</languageTerm></language>
  <location><physicalLocation type="original">University Archives</physicalLocation><shelfLocator>RS 39/2/20</shelfLocator></location>
  <identifier type="local">0001234</identifier>
  <abstract>Students gather; speakers address the crowd.</abstract>
  <accessCondition type="rights_statement">In Copyright</accessCondition>
  <note>Café scene on verso</note>
</mods>"""

# prefixed namespace + a subject name held in sub-elements
A2 = f"""<mods:mods xmlns:mods="{NS}">
  <mods:titleInfo><mods:title>Commencement  </mods:title></mods:titleInfo>
  <mods:subject><mods:name><mods:namePart>Doe, Jane</mods:namePart></mods:name></mods:subject>
  <mods:subject><mods:topic>Commencement ceremonies</mods:topic></mods:subject>
  <mods:relatedItem type="host"><mods:titleInfo><mods:title>Photo Collection</mods:title></mods:titleInfo></mods:relatedItem>
</mods:mods>"""


class E:
    def __init__(self, ref, title, etype, docs):
        self.reference, self.title, self.entity_type = ref, title, etype
        self.docs = list(docs)          # list of xml strings (all MODS)
    @property
    def metadata(self):
        return {f"https://x/api/entity/{self.reference}/metadata/{i}": NS for i, _ in enumerate(self.docs)}
    def light(self):
        l = E(self.reference, self.title, self.entity_type, [])
        l.__class__ = Light
        return l

class Light(E):
    @property
    def metadata(self):
        return None


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


class FakeClient:
    def __init__(self):
        A, F = EntityType.ASSET, EntityType.FOLDER
        self.store = {
            uid(1): E(uid(1), "protest_001.tif", A, [A1]),
            uid(2): E(uid(2), "commencement_014.tif", A, [A2]),
            uid(3): E(uid(3), "untitled_scan.tif", A, []),
            uid(4): E(uid(4), "two_records.tif", A, [A2, A2]),
            uid(10): E(uid(10), "Photo Collection", F, []),
            uid(11): E(uid(11), "Box 2", F, []),
        }
        self.children = {uid(10): [uid(1), uid(2), uid(11)], uid(11): [uid(3), uid(4)]}
        self.calls = []

    def _get(self, ref, etype):
        e = self.store[ref]
        assert e.entity_type == etype, "wrong entity type requested"
        return e
    def asset(self, ref):  return self._get(ref, EntityType.ASSET)
    def folder(self, ref): return self._get(ref, EntityType.FOLDER)
    def entity(self, etype, ref): return self._get(ref, etype)
    def descendants(self, folder=None):
        ref = folder if isinstance(folder, str) else folder.reference
        for r in self.children.get(ref, []):
            yield self.store[r].light()
    def all_descendants(self, folder=None):
        for e in self.descendants(folder):
            yield e
            if e.entity_type == EntityType.FOLDER:
                yield from self.all_descendants(e)
    def metadata_for_entity(self, entity, schema):
        assert entity.metadata is not None
        return entity.docs[0] if entity.docs and schema == NS else None
    def update_metadata(self, entity, schema, data):
        if schema not in entity.metadata.values():
            raise RuntimeError("Only existing schema's can be updated.")
        # pyPreservica parses the string with ElementTree and re-serialises it: do the same
        out = ET.tostring(ET.fromstring(data), encoding="unicode")
        entity.docs = [out for _ in entity.docs]
        self.calls.append(("update", entity.reference))
        return entity
    def add_metadata(self, entity, schema, data):
        entity.docs.append(ET.tostring(ET.fromstring(data), encoding="unicode"))
        self.calls.append(("add", entity.reference))
        return entity
