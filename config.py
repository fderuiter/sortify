# config.py

MAX_FOLDERS = 12
MAX_WORKERS = 15 # Threads for reading files quickly

# ML Settings
MIN_DF = 2 # A word must appear in at least 2 different files to be considered a "connection"
MAX_DF = 0.85 # Ignore words that appear in over 85% of files (they are too generic)

STOP_WORDS = {
    'the', 'and', 'for', 'this', 'that', 'with', 'from', 'inc', 'com', 'pdf', 'docx', 'txt', 'csv', 
    'xlsx', 'xls', 'site', 'team', 'page', 'nan', 'unnamed', 'your', 'have', 'will', 'are', 'not', 
    'can', 'all', 'was', 'has', 'but', 'what', 'there', 'out', 'about', 'get', 'would', 'like', 
    'which', 'their', 'when', 'who', 'some', 'how', 'these', 'into', 'other', 'could', 'than', 
    'only', 'also', 'over', 'well', 'because', 'through', 'don', 'should', 'been', 'much', 'where'
}