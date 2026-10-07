from io import BytesIO

from pypdf import PdfReader

from azure_sql_agents.board_report import board_pdf


def test_public_briefing_has_scope_and_responsibilities_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No network allowed')
    monkeypatch.setattr('socket.socket', forbidden)
    reader = PdfReader(BytesIO(board_pdf()))
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 4 <= len(reader.pages) <= 5
    for phrase in ('SQLIQ', 'NOT ASSESSED', 'Provider', 'Customer', 'Token efficiency',
                   'separate exact signed approval', '800-52'):
        assert phrase in text
    assert '/JavaScript' not in str(reader.trailer)


def test_company_branding_embeds_both_logos_on_every_page(tmp_path):
    from PIL import Image
    logo = tmp_path / 'company.png'
    Image.new('RGB', (300, 90), '#234577').save(logo)
    reader = PdfReader(BytesIO(board_pdf(logo, 'Example Finance Company')))
    for page in reader.pages:
        assert 'Example Finance Company' in page.extract_text()
        assert len(page.images) == 2


def test_logo_rejects_untrusted_formats_and_oversized_files(tmp_path):
    import pytest
    logo = tmp_path / 'logo.svg'
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    with pytest.raises(ValueError):
        board_pdf(logo)
    logo.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='2 MiB'):
        board_pdf(logo)


def test_company_name_validation():
    import pytest
    for name in (' ', 'x' * 81, 'Injected\nheading'):
        with pytest.raises(ValueError):
            board_pdf(company_name=name)
