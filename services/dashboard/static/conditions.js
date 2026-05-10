/**
 * services/dashboard/static/conditions.js — Conditions panel logic.
 *
 * PURPOSE:
 *   Fetches and renders environmental conditions (time periods, sunrise/sunset,
 *   weather) in the dashboard's Conditions panel. Polls the server every 5 min
 *   since conditions change slowly.
 *
 * RELATIONSHIPS:
 *   - REST: /api/conditions (astral time data + optional OpenWeatherMap)
 *   - HTML: #conditionsPanel in index.html
 *   - Data source: contracts/time_rules.py (server-side, via astral library)
 */

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------
const conditionsPeriod = document.getElementById("conditionsPeriod");
const nightWatchIcon = document.getElementById("nightWatchIcon");
const condDate = document.getElementById("condDate");
const condSunrise = document.getElementById("condSunrise");
const condSunset = document.getElementById("condSunset");
const condDayLength = document.getElementById("condDayLength");
const conditionsSchedule = document.getElementById("conditionsSchedule");

// Weather elements (hidden until data arrives)
const condWeatherItem = document.getElementById("condWeatherItem");
const condWeatherIcon = document.getElementById("condWeatherIcon");
const condWeatherLabel = document.getElementById("condWeatherLabel");
const condWeatherValue = document.getElementById("condWeatherValue");
const condWindItem = document.getElementById("condWindItem");
const condWindValue = document.getElementById("condWindValue");
const condVisItem = document.getElementById("condVisItem");
const condVisValue = document.getElementById("condVisValue");
const condFeelsLikeItem = document.getElementById("condFeelsLikeItem");
const condFeelsLikeValue = document.getElementById("condFeelsLikeValue");
const condHumidityItem = document.getElementById("condHumidityItem");
const condHumidityValue = document.getElementById("condHumidityValue");
const condCloudsItem = document.getElementById("condCloudsItem");
const condCloudsValue = document.getElementById("condCloudsValue");


// ---------------------------------------------------------------------------
// Period display helpers
// ---------------------------------------------------------------------------
const PERIOD_LABELS = {
    daytime: "☀️ Daytime",
    twilight: "🌅 Twilight",
    night: "🌙 Night",
    late_night: "🌑 Late Night",
};

/**
 * Map OpenWeatherMap icon codes to emoji.
 */
function weatherEmoji(iconCode) {
    const map = {
        "01d": "☀️", "01n": "🌙",
        "02d": "⛅", "02n": "☁️",
        "03d": "☁️", "03n": "☁️",
        "04d": "☁️", "04n": "☁️",
        "09d": "🌧️", "09n": "🌧️",
        "10d": "🌦️", "10n": "🌧️",
        "11d": "⛈️", "11n": "⛈️",
        "13d": "🌨️", "13n": "🌨️",
        "50d": "🌫️", "50n": "🌫️",
    };
    return map[iconCode] || "🌡️";
}


// ---------------------------------------------------------------------------
// Fetch + Render
// ---------------------------------------------------------------------------
async function loadConditions() {
    try {
        const resp = await fetch("/api/conditions");
        if (!resp.ok) return;
        const data = await resp.json();

        // Current period badge
        conditionsPeriod.textContent = PERIOD_LABELS[data.current_period] || data.current_period;
        conditionsPeriod.className = "conditions-period period-" + data.current_period;

        // Show night watch icon during night/late_night (suppression disabled)
        if (nightWatchIcon) {
            const isNight = data.current_period === "night" || data.current_period === "late_night";
            nightWatchIcon.classList.toggle("visible", isNight);
        }

        // Sun data
        condSunrise.textContent = data.sunrise;
        condSunset.textContent = data.sunset;
        condDayLength.textContent = data.day_length;

        // Date in dd/mm/yyyy format
        // data.date is like "Thursday, February 20, 2026" — parse and reformat
        try {
            const parsed = new Date(data.date);
            if (!isNaN(parsed.getTime())) {
                const dd = String(parsed.getDate()).padStart(2, "0");
                const mm = String(parsed.getMonth() + 1).padStart(2, "0");
                const yyyy = parsed.getFullYear();
                condDate.textContent = `${dd}/${mm}/${yyyy}`;
            } else {
                condDate.textContent = data.date;
            }
        } catch {
            condDate.textContent = data.date;
        }

        // Weather data (only shown if API key is configured)
        if (data.weather) {
            const w = data.weather;

            condWeatherIcon.textContent = weatherEmoji(w.icon);
            condWeatherLabel.textContent = w.description;
            condWeatherValue.textContent = `${w.temp_c}°C`;
            condWeatherItem.style.display = "";

            condWindValue.textContent = `${w.wind_speed_kmh} km/h`;
            condWindItem.style.display = "";

            // Feels like
            condFeelsLikeValue.textContent = `${w.feels_like_c}°C`;
            condFeelsLikeItem.style.display = "";

            // Humidity
            condHumidityValue.textContent = `${w.humidity}%`;
            condHumidityItem.style.display = "";

            // Cloud cover
            condCloudsValue.textContent = `${w.clouds_pct}%`;
            condCloudsItem.style.display = "";

            // Show visibility only if reduced (< 10km)
            if (w.visibility_m < 10000) {
                const visKm = (w.visibility_m / 1000).toFixed(1);
                condVisValue.textContent = `${visKm} km`;
                condVisItem.style.display = "";
            } else {
                condVisItem.style.display = "none";
            }
        }

        // Time period schedule
        if (data.periods && data.periods.length > 0) {
            conditionsSchedule.innerHTML = data.periods.map(p => {
                const isActive = p.name.toLowerCase().replace(" ", "_") === data.current_period;
                return `
                    <div class="schedule-row ${isActive ? "active" : ""}">
                        <span class="schedule-name">${p.icon} ${p.name}</span>
                        <span class="schedule-time">${p.start} → ${p.end}</span>
                    </div>
                `;
            }).join("");
        }
    } catch (err) {
        console.warn("Failed to load conditions:", err);
    }
}


// ---------------------------------------------------------------------------
// Initialize — called from app.js init() or standalone
// ---------------------------------------------------------------------------
function initConditions() {
    loadConditions();
    // Refresh every 5 minutes — conditions change slowly
    setInterval(loadConditions, 5 * 60 * 1000);
}

// Auto-init when DOM is ready (works alongside app.js)
document.addEventListener("DOMContentLoaded", initConditions);
