PURPLE       = 0x9B59B6
PURPLE_DARK  = 0x6C3483
PURPLE_LIGHT = 0xBB8FCE
GREEN        = 0x2ECC71
RED          = 0xE74C3C
ORANGE       = 0xF39C12
BLUE         = 0x3498DB
GREY         = 0x95A5A6

COOLDOWN_SECONDS = 300

CATEGORY_QUESTIONS = {
    'General Support': [
        ('Subject',       'What is the subject of your issue?',        False),
        ('Description',   'Please describe your issue in detail.',      False),
    ],
    'Billing Support': [
        ('Order / Invoice #', 'What is your order or invoice number?', False),
        ('Issue',             'Describe your billing issue in detail.', False),
    ],
    'Report User': [
        ('Reported User',   'Username, display name, or ID of the user you are reporting.', False),
        ('Incident Detail', 'Please describe the incident in full detail.',                  False),
    ],
    'Partnership': [
        ('Server Name & Size', 'What is your server name and how many members do you have?', False),
        ('Proposal',           'Describe your partnership proposal.',                         False),
    ],
}
