"""WGS-84 Transverse Mercator projection utilities."""
import math


_WGS84_A = 6378137.0  # semi-major axis [m]
_WGS84_F = 1.0 / 298.257223563  # flattening
_WGS84_E2 = 2 * _WGS84_F - _WGS84_F**2  # first eccentricity squared
_WGS84_E = math.sqrt(_WGS84_E2)
_WGS84_N = _WGS84_F / (2.0 - _WGS84_F)  # third flattening

# Rectifying radius  A* = a/(1+n) * (1 + n²/4 + n⁴/64 + …)
_n = _WGS84_N
_n2 = _n * _n
_n3 = _n2 * _n
_n4 = _n3 * _n
_n5 = _n4 * _n
_n6 = _n5 * _n
_TM_A = (_WGS84_A / (1.0 + _n)) * (1.0 + _n2 / 4.0 + _n4 / 64.0 + _n6 / 256.0)

# Krüger α coefficients (indices 1..6) — forward projection
_TM_ALPHA = (
    0.0,  # placeholder so series starts at index 1
    _n / 2
    - 2 * _n2 / 3
    + 5 * _n3 / 16
    + 41 * _n4 / 180
    - 127 * _n5 / 288
    + 7891 * _n6 / 37800,
    13 * _n2 / 48
    - 3 * _n3 / 5
    + 557 * _n4 / 1440
    + 281 * _n5 / 630
    - 1983433 * _n6 / 1935360,
    61 * _n3 / 240 - 103 * _n4 / 140 + 15061 * _n5 / 26880 + 167603 * _n6 / 181440,
    49561 * _n4 / 161280 - 179 * _n5 / 168 + 6601661 * _n6 / 7257600,
    34729 * _n5 / 80640 - 3418889 * _n6 / 1995840,
    212378941 * _n6 / 319334400,
)

# Krüger β coefficients (indices 1..6) — inverse projection
_TM_BETA = (
    0.0,
    _n / 2
    - 2 * _n2 / 3
    + 37 * _n3 / 96
    - _n4 / 360
    - 81 * _n5 / 512
    + 96199 * _n6 / 604800,
    _n2 / 48 + _n3 / 15 - 437 * _n4 / 1440 + 46 * _n5 / 105 - 1118711 * _n6 / 3870720,
    17 * _n3 / 480 - 37 * _n4 / 840 - 209 * _n5 / 4480 + 5569 * _n6 / 90720,
    4397 * _n4 / 161280 - 11 * _n5 / 504 - 830251 * _n6 / 7257600,
    4583 * _n5 / 161280 - 108847 * _n6 / 3991680,
    20648693 * _n6 / 638668800,
)


def _tmerc_forward_wgs84(
    lat_deg: float,
    lon_deg: float,
    lat0_deg: float,
    lon0_deg: float,
    k0: float = 1.0,
    false_e: float = 0.0,
    false_n: float = 0.0,
) -> tuple:
    """Ellipsoidal Transverse Mercator forward projection on WGS-84.

    Uses the Karney / Krüger 6th-order series — the same algorithm as
    PROJ ``+proj=tmerc``.  Accuracy is sub-millimetre within 60° of the
    central meridian.

    Returns ``(easting, northing)`` in metres.
    """
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg) - math.radians(lon0_deg)
    phi0 = math.radians(lat0_deg)
    e = _WGS84_E

    # --- conformal latitude via exact formula ---
    sin_phi = math.sin(phi)
    tau = math.tan(phi)
    sigma = math.sinh(e * math.atanh(e * sin_phi))
    tau_p = tau * math.sqrt(1.0 + sigma * sigma) - sigma * math.sqrt(1.0 + tau * tau)

    cos_lam = math.cos(lam)
    sin_lam = math.sin(lam)

    xi_p = math.atan2(tau_p, cos_lam)
    eta_p = math.atanh(sin_lam / math.sqrt(tau_p * tau_p + cos_lam * cos_lam))

    # --- origin offset (φ₀ on central meridian, λ=0) ---
    sin_phi0 = math.sin(phi0)
    tau0 = math.tan(phi0)
    sigma0 = math.sinh(e * math.atanh(e * sin_phi0))
    tau0_p = tau0 * math.sqrt(1.0 + sigma0 * sigma0) - sigma0 * math.sqrt(
        1.0 + tau0 * tau0
    )
    xi0_p = math.atan2(tau0_p, 1.0)  # cos(0) = 1

    # --- Krüger series summation ---
    xi = xi_p
    eta = eta_p
    xi0 = xi0_p
    for j in range(1, 7):
        two_j = 2 * j
        s2j = math.sin(two_j * xi_p)
        c2j = math.cos(two_j * xi_p)
        ch2j = math.cosh(two_j * eta_p)
        sh2j = math.sinh(two_j * eta_p)
        aj = _TM_ALPHA[j]
        xi += aj * s2j * ch2j
        eta += aj * c2j * sh2j
        xi0 += aj * math.sin(two_j * xi0_p)  # cosh(0)=1

    easting = false_e + k0 * _TM_A * eta
    northing = false_n + k0 * _TM_A * (xi - xi0)
    return easting, northing


def _tmerc_inverse_wgs84(
    easting: float,
    northing: float,
    lat0_deg: float,
    lon0_deg: float,
    k0: float = 1.0,
    false_e: float = 0.0,
    false_n: float = 0.0,
) -> tuple:
    """Inverse ellipsoidal Transverse Mercator on WGS-84.

    Recovers ``(lat_deg, lon_deg)`` from ``(easting, northing)`` using the
    Karney / Krüger 6th-order inverse series.
    """
    e = _WGS84_E

    # Origin offset (xi0)
    phi0 = math.radians(lat0_deg)
    sin_phi0 = math.sin(phi0)
    tau0 = math.tan(phi0)
    sigma0 = math.sinh(e * math.atanh(e * sin_phi0))
    tau0_p = tau0 * math.sqrt(1.0 + sigma0 * sigma0) - sigma0 * math.sqrt(
        1.0 + tau0 * tau0
    )
    xi0_p = math.atan2(tau0_p, 1.0)
    xi0 = xi0_p
    for j in range(1, 7):
        xi0 += _TM_ALPHA[j] * math.sin(2 * j * xi0_p)

    # Normalised coords
    eta = (easting - false_e) / (k0 * _TM_A)
    xi = (northing - false_n) / (k0 * _TM_A) + xi0

    # Inverse Krüger series to get xi_p, eta_p
    xi_p = xi
    eta_p = eta
    for j in range(1, 7):
        two_j = 2 * j
        bj = _TM_BETA[j]
        xi_p -= bj * math.sin(two_j * xi) * math.cosh(two_j * eta)
        eta_p -= bj * math.cos(two_j * xi) * math.sinh(two_j * eta)

    # Recover tau' then phi
    sin_xi_p = math.sin(xi_p)
    cos_xi_p = math.cos(xi_p)
    sinh_eta_p = math.sinh(eta_p)
    cosh_eta_p = math.cosh(eta_p)
    # tau'_target = sin(xi') / sqrt(cos²(xi') + sinh²(eta'))
    tau_p_target = sin_xi_p / math.sqrt(cos_xi_p * cos_xi_p + sinh_eta_p * sinh_eta_p)
    # Newton iteration: tau from tau' via sigma(tau)
    tau = tau_p_target  # initial guess (conformal ≈ geodetic for small e)
    for _ in range(5):
        tau_a = math.sqrt(1.0 + tau * tau)
        sin_phi_i = tau / tau_a
        sigma_i = math.sinh(e * math.atanh(e * sin_phi_i))
        tau_p_i = tau * math.sqrt(1.0 + sigma_i * sigma_i) - sigma_i * tau_a
        dtau = (
            (tau_p_target - tau_p_i)
            * (1.0 + (1.0 - e * e) * tau * tau)
            / ((1.0 - e * e) * tau_a * math.sqrt(1.0 + tau_p_i * tau_p_i))
        )
        tau += dtau
        if abs(dtau) < 1e-12:
            break

    lat = math.degrees(math.atan(tau))
    lon = lon0_deg + math.degrees(math.atan2(sinh_eta_p, cos_xi_p))
    return lat, lon


