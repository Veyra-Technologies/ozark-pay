from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CABINET = (ROOT / "app/templates/cabinet.html").read_text(encoding="utf-8")
TEAMLEAD = (ROOT / "app/templates/teamlead.html").read_text(encoding="utf-8")


def test_teamlead_static_helper_banners_are_absent():
    assert (
        "TeamLead получает отдельный расход платформы от gross успешного Deposit."
        not in CABINET
    )
    assert (
        "Для супер-админа: процент TeamLead может превысить маржу сделки"
        not in CABINET
    )
    assert (
        "В момент отправки backend получает свежий live askPrice Rapira"
        not in TEAMLEAD
    )


def test_ai_office_static_banner_and_duplicate_heading_are_absent():
    assert "Интеграция не должна включаться в production" not in CABINET
    assert "<h3>Интеграция с AI-офисом</h3>" not in CABINET
    assert "<h3>Настройки подключения</h3>" in CABINET


def test_ai_office_activation_warning_is_dynamic_and_specific():
    assert "data-ai-office-activation-error hidden" in CABINET
    assert "ai_office_m2m_auth_required" in CABINET
    assert "ai_office_credential_required" in CABINET
    assert "Выберите обязательный M2M Auth type" in CABINET
    assert (
        "Добавьте credential для выбранного M2M Auth type"
        in CABINET
    )


def test_rolling_confirm_and_dispute_forms_keep_distinct_contracts():
    rolling_start = CABINET.index(
        "{% set pending_rolling_transfers = "
    )
    rolling_end = CABINET.index(
        "{% if mro.has_confirmed_rolling %}",
        rolling_start,
    )
    rolling_block = CABINET[rolling_start:rolling_end]
    confirm_start = rolling_block.index(
        'id="rolling-transfer-confirm-'
    )
    dispute_start = rolling_block.index(
        'id="rolling-transfer-dispute-',
        confirm_start,
    )
    confirm_block = rolling_block[confirm_start:dispute_start]
    dispute_block = rolling_block[dispute_start:]

    assert 'textarea name="reason"' not in confirm_block
    assert 'textarea name="reason" required' in dispute_block
    assert (
        "selectattr('status','equalto','pending_confirmation')"
        in rolling_block
    )


def test_rolling_values_are_formatted_or_shortened_only_for_display():
    assert (
        '"{:,.2f}".format(transfer.amount_usdt|float)'
        in CABINET
    )
    assert "transfer.sent_at|dt_compact" in CABINET
    assert (
        'data-copy-value="{{ transfer.destination_address }}"'
        in CABINET
    )
    assert 'data-copy-value="{{ transfer.tx_hash }}"' in CABINET
    assert (
        "{{ transfer.destination_address[:8] }}…"
        "{{ transfer.destination_address[-8:] }}"
        in CABINET
    )
    assert (
        "{{ transfer.tx_hash[:8] }}…"
        "{{ transfer.tx_hash[-8:] if transfer.tx_hash else '—' }}"
        in CABINET
    )


def test_rapira_source_is_humanized_and_mock_badge_is_rehearsal_only():
    assert (
        "{% if rapira_rate.rate_source == 'rapira_live' %}"
        "Rapira{% else %}{{ rapira_rate.rate_source }}{% endif %}"
        in CABINET
    )
    assert (
        "transfer_source_label = 'Rapira' "
        "if transfer_source == 'rapira_live' else transfer_source"
        in CABINET
    )
    assert CABINET.count(
        "{% if is_rehearsal_environment %}"
    ) >= 2
    assert CABINET.count("QA MOCK") == 2
    assert (
        "is_rehearsal_environment = "
        "'rehearsal' in (environment_label | lower)"
        in CABINET
    )
