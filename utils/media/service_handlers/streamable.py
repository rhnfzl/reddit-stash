import re
import logging
import requests
from ..media_core import ignore_ssl_errors

def extract_streamable_video_url(url):
    """Extract the direct video URL from a Streamable URL."""
    try:
        # Extract the streamable ID
        streamable_id_match = re.search(r'streamable\.com/([a-zA-Z0-9]+)', url)
        if not streamable_id_match:
            return None
            
        streamable_id = streamable_id_match.group(1)
        
        # Get the video info from the API
        api_url = f"https://api.streamable.com/videos/{streamable_id}"
        response = requests.get(api_url)
        response.raise_for_status()
        
        data = response.json()
        if 'files' in data and 'mp4' in data['files']:
            return f"https://streamable.com/{data['files']['mp4']['url']}"
            
        return None
    except Exception as e:
        logging.error(f"Failed to extract Streamable video URL: {e}")
        return None 