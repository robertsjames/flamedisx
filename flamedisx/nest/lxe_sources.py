import tensorflow as tf

import configparser
import os

import flamedisx as fd
from .. import nest as fd_nest

export, __all__ = fd.exporter()
o = tf.newaxis

GAS_CONSTANT = 8.314
N_AVAGADRO = 6.0221409e23
A_XENON = 131.293
XENON_LIQUID_DIELECTRIC = 1.85
XENON_GAS_DIELECTRIC = 1.00126


class nestSource(fd.BlockModelSource):
    def __init__(self, *args, detector='default', **kwargs):
        assert detector in ('default',)

        assert os.path.exists(os.path.join(
            os.path.dirname(__file__), 'config/', detector + '.ini'))

        config = configparser.ConfigParser(inline_comment_prefixes=';')
        config.read(os.path.join(os.path.dirname(__file__), 'config/', detector + '.ini'))

        # common (known) parameters
        self.temperature = config.getfloat('NEST', 'temperature_config')
        self.pressure = config.getfloat('NEST', 'pressure_config')
        self.drift_field = config.getfloat('NEST', 'drift_field_config')
        self.gas_field = config.getfloat('NEST', 'gas_field_config')

        # derived (known) parameters
        self.density = fd_nest.calculate_density(
            self.temperature, self.pressure)
        # NOTE: BE CAREFUL WITH THE BELOW, ONLY VALID NEAR VAPOUR PRESSURE!!!
        self.density_gas = fd_nest.calculate_density_gas(
            self.temperature, self.pressure)
        #
        self.drift_velocity = fd_nest.calculate_drift_velocity(
            self.drift_field, self.density, self.temperature)

        # energy_spectrum.py
        self.radius = config.getfloat('NEST', 'radius_config')
        self.z_topDrift = config.getfloat('NEST', 'z_topDrift_config')
        self.z_top = self.z_topDrift - self.drift_velocity * \
            config.getfloat('NEST', 'dt_min_config')
        self.z_bottom = self.z_topDrift - self.drift_velocity * \
            config.getfloat('NEST', 'dt_max_config')

        # detection.py
        self.g1 = config.getfloat('NEST', 'g1_config')
        self.min_photons = config.getint('NEST', 'min_photons_config')
        self.elife = config.getint('NEST', 'elife_config')

        # secondary_quanta_generation.py
        self.gas_gap = config.getfloat('NEST', 'gas_gap_config')
        self.g1_gas = config.getfloat('NEST', 'g1_gas_config')
        self.s2Fano = config.getfloat('NEST', 's2Fano_config')

        # double_pe.py
        self.double_pe_fraction = config.getfloat(
            'NEST', 'double_pe_fraction_config')

        # pe_detection.py
        self.spe_eff = config.getfloat('NEST', 'spe_eff_config')
        self.num_pmts = config.getfloat('NEST', 'num_pmts_config')

        # final_signals.py
        self.spe_res = config.getfloat('NEST', 'spe_res_config')
        self.S1_noise = config.getfloat('NEST', 'S1_noise_config')
        self.S2_noise = config.getfloat('NEST', 'S2_noise_config')

        self.S1_min = config.getfloat('NEST', 'S1_min_config')
        self.S1_max = config.getfloat('NEST', 'S1_max_config')
        self.S2_min = config.getfloat('NEST', 'S2_min_config')
        self.S2_max = config.getfloat('NEST', 'S2_max_config')

        super().__init__(*args, **kwargs)

    final_dimensions = ('s1', 's2')
    penultimate_dimensions = ('s1_photoelectrons_detected',
                              's2_photoelectrons_detected')
    no_step_dimensions = ('s1_photoelectrons_produced',
                          's1_photoelectrons_detected')

    # detection.py

    def photon_detection_eff(self, z):
        return self.g1 * tf.ones_like(z)

    def electron_detection_eff(self, drift_time):
        liquid_field_interface = self.gas_field / \
            (XENON_LIQUID_DIELECTRIC / XENON_GAS_DIELECTRIC)
        extraction_eff = -0.03754 * liquid_field_interface**2 + \
            0.52660 * liquid_field_interface - 0.84645

        return extraction_eff * tf.exp(-drift_time / self.elife)

    def s2_photon_detection_eff(self, z):
        return self.g1_gas * tf.ones_like(z)

    # secondary_quanta_generation.py

    def electron_gain_mean(self):
        elYield = (
            0.137 * self.gas_field * 1e3 -
            4.70e-18 * (N_AVAGADRO * self.density_gas / A_XENON)) \
            * self.gas_gap * 0.1

        return tf.cast(elYield, fd.float_type())[o]

    def electron_gain_std(self):
        elYield = (
            0.137 * self.gas_field * 1e3 -
            4.70e-18 * (N_AVAGADRO * self.density_gas / A_XENON)) \
            * self.gas_gap * 0.1

        return tf.sqrt(self.s2Fano * elYield)[o]

    # pe_detection.py

    def photoelectron_detection_eff(self, pe_det):
        eff = tf.where(
            self.spe_eff < 1.,
            self.spe_eff + (1. - self.spe_eff) / (2. * self.num_pmts) * pe_det,
            self.spe_eff)
        eff_trunc = tf.where(
            eff > 1.,
            1.,
            eff)

        return 1. - (1. - eff_trunc) / (1. + self.double_pe_fraction)

    # final_signals.py

    def s1_spe_smearing(self, n_pe):
        return tf.sqrt(
            self.spe_res * self.spe_res * n_pe +
            self.S1_noise * self.S1_noise * n_pe * n_pe)

    def s2_spe_smearing(self, n_pe):
        return tf.sqrt(
            self.spe_res * self.spe_res * n_pe +
            self.S2_noise * self.S2_noise * n_pe * n_pe)


@export
class nestERSource(nestSource):
    def __init__(self, *args, energy_min=0., energy_max=10., num_energies=1000, **kwargs):
        self.energies = tf.cast(tf.linspace(energy_min, energy_max, num_energies),
                                fd.float_type())
        self.rates_vs_energy = tf.ones(num_energies, fd.float_type())

        super().__init__(*args, **kwargs)

        # quanta_splitting.py
        self.Wq_keV = fd_nest.calculate_work(self.density)

    model_blocks = (
        fd_nest.FixedShapeEnergySpectrum,
        fd_nest.MakePhotonsElectronER,
        fd_nest.DetectPhotons,
        fd_nest.MakeS1Photoelectrons,
        fd_nest.DetectS1Photoelectrons,
        fd_nest.MakeS1,
        fd_nest.DetectElectrons,
        fd_nest.MakeS2Photons,
        fd_nest.DetectS2Photons,
        fd_nest.MakeS2Photoelectrons,
        fd_nest.MakeS2)

    final_dimensions = ('s1', 's2')
    no_step_dimensions = ('s1_photoelectrons_produced',
                          's1_photoelectrons_detected')

    def mean_yield_electron(self, energy):
        Wq_eV = self.Wq_keV * 1e3

        QyLvllowE = 1e3 / Wq_eV + 6.5 * (1. - 1. / (1. + pow(self.drift_field / 47.408, 1.9851)))
        HiFieldQy = 1. + 0.4607 / pow(1. + pow(self.drift_field / 621.74, -2.2717), 53.502)
        QyLvlmedE = 32.988 -  32.988 / (1. + pow(self.drift_field / (0.026715 * tf.exp(self.density / 0.33926)), 0.6705))
        QyLvlmedE *= HiFieldQy
        DokeBirks = 1652.264 + (1.415935e10 - 1652.264) / (1. + pow(self.drift_field / 0.02673144, 1.564691))
        LET_power = -2.
        QyLvlhighE = 28.
        Qy = QyLvlmedE + (QyLvllowE - QyLvlmedE) / pow(1. + 1.304 * pow(energy, 2.1393), 0.35535) + QyLvlhighE / (1. + DokeBirks * pow(energy, LET_power))

        nel_temp = Qy * energy
        # Don't let number of electrons go negative
        nel = tf.where(nel_temp < 0,
                       0 * nel_temp,
                       nel_temp)

        return nel

    def mean_yield_quanta(self, *args):
        energy = args[0]
        nel_mean = args[1]

        nq_temp = energy / self.Wq_keV

        nph_temp = nq_temp - nel_mean
        # Don't let number of photons go negative
        nph = tf.where(nph_temp < 0,
                       0 * nph_temp,
                       nph_temp)

        nq = nel_mean + nph

        return nq

    def fano_factor(self, nq):
        Fano = 0.12707 - 0.029623 * self.density - 0.0057042 * pow(self.density, 2.) + 0.0015957 * pow(self.density, 3.)

        return Fano + 0.0015 * tf.sqrt(nq) * pow(self.drift_field, 0.5)

    def alpha(self, energy):
        alf = 0.067366 + self.density * 0.039693
        excitonR = alf * tf.math.erf(0.05 * energy)

        return 1. / (1. + excitonR)


    @staticmethod
    def p_electron(nq, *, er_pel_a=15, er_pel_b=-27.7, er_pel_c=32.5,
                   er_pel_e0=5.):
        """Fraction of ER quanta that become electrons
        Simplified form from Jelle's thesis
        """
        # The original model depended on energy, but in flamedisx
        # it has to be a direct function of nq.
        e_kev_sortof = nq * 13.7e-3
        eps = fd.tf_log10(e_kev_sortof / er_pel_e0 + 1e-9)
        qy = (
            er_pel_a * eps ** 2
            + er_pel_b * eps
            + er_pel_c)
        return fd.safe_p(qy * 13.7e-3)


@export
class nestNRSource(nestSource):
    def __init__(self, *args, energy_min=0.7, energy_max=150., num_energies=1000, **kwargs):
        self.energies = tf.cast(tf.linspace(energy_min, energy_max, num_energies),
                                fd.float_type())
        self.rates_vs_energy = tf.ones(num_energies, fd.float_type())
        super().__init__(*args, **kwargs)

    model_blocks = (
        fd_nest.FixedShapeEnergySpectrum,
        fd_nest.MakePhotonsElectronsNR,
        fd_nest.DetectPhotons,
        fd_nest.MakeS1Photoelectrons,
        fd_nest.DetectS1Photoelectrons,
        fd_nest.MakeS1,
        fd_nest.DetectElectrons,
        fd_nest.MakeS2Photons,
        fd_nest.DetectS2Photons,
        fd_nest.MakeS2Photoelectrons,
        fd_nest.MakeS2)

    @staticmethod
    def p_electron(nq, *,
                   alpha=1.280, zeta=0.045, beta=273 * .9e-4,
                   gamma=0.0141, delta=0.062,
                   drift_field=120):
        """Fraction of detectable NR quanta that become electrons,
        slightly adjusted from Lenardo et al.'s global fit
        (https://arxiv.org/abs/1412.4417).
        Penning quenching is accounted in the photon detection efficiency.
        """
        # TODO: so to make field pos-dependent, override this entire f?
        # could be made easier...

        # prevent /0  # TODO can do better than this
        nq = nq + 1e-9

        # Note: final term depends on nq now, not energy
        # this means beta is different from lenardo et al
        nexni = alpha * drift_field ** -zeta * (1 - tf.exp(-beta * nq))
        ni = nq * 1 / (1 + nexni)

        # Fraction of ions NOT participating in recombination
        squiggle = gamma * drift_field ** -delta
        fnotr = tf.math.log(1 + ni * squiggle) / (ni * squiggle)

        # Finally, number of electrons produced..
        n_el = ni * fnotr

        return fd.safe_p(n_el / nq)


@export
class nestSpatialRateERSource(nestERSource):
    model_blocks = (fd_nest.SpatialRateEnergySpectrum,) + nestERSource.model_blocks[1:]


@export
class nestSpatialRateNRSource(nestNRSource):
    model_blocks = (fd_nest.SpatialRateEnergySpectrum,) + nestNRSource.model_blocks[1:]


@export
class nestWIMPSource(nestNRSource):
    model_blocks = (fd_nest.WIMPEnergySpectrum,) + nestNRSource.model_blocks[1:]
