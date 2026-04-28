ZONE_HOME = {
    "tl_y": 39.796827, # Top-Left Latitude (deg) https://www.latlong.net/ or google maps. The bigger the zone, the more planes you'll get. My zone is ~3.5 miles in each direction or 10mi corner to corner. 
    "tl_x": -105.298648, # Top-Left Longitude (deg)
    "br_y": 39.666880, # Bottom-Right Latitude (deg)
    "br_x": -105.110076 # Bottom-Right Longitude (deg)
}
LOCATION_HOME = [
    39.725715, # Latitude (deg)
    -105.203208 # Longitude (deg)
]
TEMPERATURE_LOCATION = "39.725715,-105.203208" #same as location home
TOMORROW_API_KEY = "sfvZeaLaJjjO7Q3cWjkHgKmZ10LM7exU" # Get an API key from https://tomorrow.io they only allows 25 pulls an hour, if you reach the limit you'll need to wait until the next hour 
TEMPERATURE_UNITS = "imperial" #can use "metric" if you want, same for distance 
DISTANCE_UNITS = "imperial"
CLOCK_FORMAT = "12hr" #use 12hr or 24hr
MIN_ALTITUDE = 8000 #feet above sea level. If you live at 1000ft then you'd want to make yours ~3000 etc. I use 2000 to weed out some of the smaller general aviation traffic. 
BRIGHTNESS = 100
BRIGHTNESS_NIGHT = 50
NIGHT_BRIGHTNESS = True #True for on False for off
NIGHT_START = "18:00" #dims screen between these hours
NIGHT_END = "07:00"
GPIO_SLOWDOWN = 2 #depends what Pi you have I use 2 for Pi 3 and 1 for Pi Zero
JOURNEY_CODE_SELECTED = "DEN" #your home airport code ALL CAPS ie ORD
JOURNEY_BLANK_FILLER = " ? " #what to display if theres no airport code
HAT_PWM_ENABLED = False #only if you haven't soldered the PWM bridge use True if you did
FORECAST_DAYS = 3 #today plus the next two days
EMAIL = "rdhobbs@gmail.com" #insert your email address between the " ie "example@example.com" to recieve emails when there is a new top 3 flight. Leave "" to recieve no emails. It will log/local webpage regardless
MAX_FARTHEST = 5 #the amount of furthest flights you want in your log
MAX_CLOSEST = 5 #the amount of closest flights to your house you want in your log

