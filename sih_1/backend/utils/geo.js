/**
 * Geospatial Utility Functions
 * Helpers for MongoDB geospatial queries and distance calculations.
 */

const EARTH_RADIUS_KM = 6371;

/**
 * Convert kilometers to radians (for MongoDB $centerSphere queries)
 * @param {number} km - Distance in kilometers
 * @returns {number} Distance in radians
 */
const kmToRadians = (km) => {
  return km / EARTH_RADIUS_KM;
};

/**
 * Convert kilometers to meters
 * @param {number} km - Distance in kilometers
 * @returns {number} Distance in meters
 */
const kmToMeters = (km) => {
  return km * 1000;
};

/**
 * Build a MongoDB $near query for finding documents near a point.
 * Uses $nearSphere with $maxDistance in meters (works with 2dsphere index).
 *
 * @param {number} lng - Longitude
 * @param {number} lat - Latitude
 * @param {number} radiusKm - Search radius in kilometers
 * @returns {Object} MongoDB query object for the `location` field
 */
const buildNearQuery = (lng, lat, radiusKm) => {
  return {
    location: {
      $nearSphere: {
        $geometry: {
          type: 'Point',
          coordinates: [parseFloat(lng), parseFloat(lat)],
        },
        $maxDistance: kmToMeters(radiusKm),
      },
    },
  };
};

/**
 * Build a MongoDB $geoWithin query using $centerSphere.
 * Alternative to $near when sorting by distance is not needed.
 *
 * @param {number} lng - Longitude
 * @param {number} lat - Latitude
 * @param {number} radiusKm - Search radius in kilometers
 * @returns {Object} MongoDB query object for the `location` field
 */
const buildWithinQuery = (lng, lat, radiusKm) => {
  return {
    location: {
      $geoWithin: {
        $centerSphere: [[parseFloat(lng), parseFloat(lat)], kmToRadians(radiusKm)],
      },
    },
  };
};

/**
 * Calculate the Haversine distance between two coordinate pairs.
 *
 * @param {number[]} coord1 - [longitude, latitude]
 * @param {number[]} coord2 - [longitude, latitude]
 * @returns {number} Distance in kilometers
 */
const calculateDistance = (coord1, coord2) => {
  const [lng1, lat1] = coord1;
  const [lng2, lat2] = coord2;

  const dLat = toRadians(lat2 - lat1);
  const dLng = toRadians(lng2 - lng1);

  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRadians(lat1)) *
      Math.cos(toRadians(lat2)) *
      Math.sin(dLng / 2) *
      Math.sin(dLng / 2);

  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_KM * c;
};

/**
 * Convert degrees to radians
 * @param {number} degrees
 * @returns {number} Radians
 */
const toRadians = (degrees) => {
  return (degrees * Math.PI) / 180;
};

/**
 * Validate that coordinates are within valid ranges.
 * @param {number} lng - Longitude (-180 to 180)
 * @param {number} lat - Latitude (-90 to 90)
 * @returns {boolean}
 */
const isValidCoordinate = (lng, lat) => {
  const lngNum = parseFloat(lng);
  const latNum = parseFloat(lat);
  return (
    !isNaN(lngNum) &&
    !isNaN(latNum) &&
    lngNum >= -180 &&
    lngNum <= 180 &&
    latNum >= -90 &&
    latNum <= 90
  );
};

module.exports = {
  EARTH_RADIUS_KM,
  kmToRadians,
  kmToMeters,
  buildNearQuery,
  buildWithinQuery,
  calculateDistance,
  isValidCoordinate,
};
