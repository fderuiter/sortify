import logging
from config import LOG_FILE

def setup_logger():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        force=True
    )
    return logging.getLogger("AutoSorter")

logger = setup_logger()
