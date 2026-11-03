"""Disposable subprocess probe: never run corrupt TZif parsers in the test runner."""
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zoneinfo

from super_super_harness.reminders import LibcZone, local_zone, time_context


def main(mode):
    source = next((Path(root) / 'Asia/Shanghai' for root in zoneinfo.TZPATH
                   if (Path(root) / 'Asia/Shanghai').is_file()), None)
    if source is None:
        raise RuntimeError('This regression needs a real Asia/Shanghai TZif in zoneinfo.TZPATH')
    data = source.read_bytes()
    assert data.startswith(b'TZif') and data[4:5] >= b'2' and data.endswith(b'\n')
    errors = []
    with tempfile.TemporaryDirectory(prefix='ssharness-tzif-') as folder:
        root = Path(folder)
        path = root / 'Asia' / 'Shanghai'
        path.parent.mkdir()
        zoneinfo.reset_tzpath([str(root)])
        name = ':' + str(path) if mode == 'absolute' else 'Asia/Shanghai'
        key = str(path) if mode == 'absolute' else name
        with patch.dict(os.environ, TZ=name):
            # Every prefix, not a sample: this includes every byte of the footer,
            # its opening newline, and all preceding binary/header boundaries.
            for cut in range(len(data)):
                path.write_bytes(data[:cut])
                zoneinfo.ZoneInfo.clear_cache()
                print(json.dumps({'mode': mode, 'cut': cut}), flush=True)
                try:
                    zone = local_zone()
                    assert isinstance(zone, LibcZone), 'truncated prefix did not fall back'
                    assert 'now' in time_context(1700000000)
                except Exception as error:
                    errors.append({'cut': cut, 'error': type(error).__name__, 'detail': str(error)})
                    print(json.dumps(errors[-1]), flush=True)
            path.write_bytes(data)
            zoneinfo.ZoneInfo.clear_cache()
            zone = local_zone()
            assert isinstance(zone, zoneinfo.ZoneInfo)
            assert zone.key == key
            context = time_context(1700000000)
            assert context['timezone'] == key
            assert context['now'].endswith('+08:00')
            assert zone.utcoffset(datetime(2026, 1, 1)) == \
                zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key=key).utcoffset(datetime(2026, 1, 1))
            path.write_bytes(data[:-1])
            with patch('super_super_harness.reminders.ZoneInfo') as parser:
                assert isinstance(local_zone(), LibcZone)
                parser.assert_not_called()
                parser.from_file.assert_not_called()
    print(json.dumps({'mode': mode, 'source_bytes': len(data), 'cuts_checked': len(data),
                      'last_cut': len(data) - 1, 'normal_key': key, 'errors': errors}), flush=True)
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
