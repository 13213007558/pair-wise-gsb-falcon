import csv
import io

import falcon


class CsvHandler(falcon.media.BaseHandler):
    """Serialize a sequence of rows to comma-separated values."""

    def serialize(self, media, content_type):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerows(media)
        return buffer.getvalue().encode('utf-8')


@falcon.provides('application/json', 'text/csv')
class SensorResource:
    """Serve sensor readings as JSON or CSV, based on the Accept header."""

    def on_get(self, req, resp):
        resp.media = [
            ['timestamp', 'temperature'],
            ['2026-01-01T00:00:00Z', '21.5'],
            ['2026-01-01T01:00:00Z', '20.8'],
        ]


app = falcon.App()
app.resp_options.media_handlers['text/csv'] = CsvHandler()
app.add_route('/sensors', SensorResource())
