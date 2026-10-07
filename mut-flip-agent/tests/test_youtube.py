"""Upload parsing must not block the feeder when YouTube changes metadata."""
import json
import subprocess
import sys

from app.youtube import parse_videos_tab


def lockup(uid, title, age='1d ago'):
    return {'lockupViewModel': {
        'contentId': uid, 'contentType': 'LOCKUP_CONTENT_TYPE_VIDEO',
        'metadata': {'lockupMetadataViewModel': {
            'title': {'content': title},
            'metadata': {'contentMetadataViewModel': {'metadataRows': [{'metadataParts': [
                {'text': {'content': '6.8K'}, 'accessibilityLabel': '6.8 thousand views',
                 'leadingIcon': {'name': 'PLAY_ARROW_OUTLINED'}},
                {'text': {'content': age}, 'accessibilityLabel': '1 day ago'},
            ]}]}}}}}}


def test_extra_metadata_fields_keep_ids_titles_order_and_deduplication():
    items = [lockup('a' * 11, 'A "quoted" title'), lockup('b' * 11, 'Second'),
             lockup('a' * 11, 'Duplicate')]
    page = '<meta property="og:title" content="Channel &amp; Co"><script>var ytInitialData = ' + json.dumps(items) + ';</script>'
    videos = parse_videos_tab(page)
    assert [(v.id, v.title, v.channel, v.age) for v in videos] == [
        ('a' * 11, 'A "quoted" title', 'Channel & Co', '1d ago'),
        ('b' * 11, 'Second', 'Channel & Co', '1d ago')]


def test_missing_or_malformed_embedded_data_is_empty():
    assert parse_videos_tab('<html>No uploads</html>') == []
    assert parse_videos_tab('var ytInitialData = {broken') == []
    page = 'window["ytInitialData"] = ' + json.dumps([lockup('c' * 11, 'Third')])
    assert parse_videos_tab(page, 'Explicit channel')[0].channel == 'Explicit channel'


def test_large_changed_page_finishes_without_cross_page_regex_backtracking():
    # The previous parser stalls on this metadata layout. Run in a subprocess so
    # a regression cannot hold pytest's GIL indefinitely.
    page = 'var ytInitialData = ' + json.dumps(
        [lockup(f'{i:011d}', 'Upload ' + str(i)) for i in range(150)], separators=(',', ':'))
    program = 'import sys; from app.youtube import parse_videos_tab; print(len(parse_videos_tab(sys.stdin.read())))'
    result = subprocess.run([sys.executable, '-c', program], input=page, text=True,
                            capture_output=True, timeout=3, check=True)
    assert result.stdout.strip() == '150'
