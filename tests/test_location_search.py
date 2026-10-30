import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from location_search import geocode

class LocationSearch(unittest.TestCase):
    def fetch(self, url):
        return {'results': [
            {'name':'Phoenix','latitude':33.4484,'longitude':-112.074,'admin1':'Arizona','country':'United States','country_code':'US'},
            {'name':'Phoenix','latitude':-29.7,'longitude':31.0,'admin1':'KwaZulu-Natal','country':'South Africa','country_code':'ZA'},
        ]}

    def test_city_region_country_used_by_published_example(self):
        p = geocode('Phoenix, Arizona, USA', self.fetch)
        self.assertEqual(p['latitude'], 33.4484)
        self.assertEqual(p['longitude'], -112.074)

    def test_city_country_and_alias(self):
        self.assertEqual(geocode('Phoenix, USA', self.fetch)['latitude'], 33.4484)
        self.assertEqual(geocode('Phoenix, US', self.fetch)['latitude'], 33.4484)

    def test_region_and_country_must_both_match(self):
        with self.assertRaises(ValueError):
            geocode('Phoenix, Arizona, South Africa', self.fetch)

    def test_region_selects_same_named_city(self):
        self.assertEqual(geocode('Phoenix, KwaZulu-Natal, South Africa', self.fetch)['longitude'], 31.0)
