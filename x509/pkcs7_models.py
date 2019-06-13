#*    pyx509 - Python library for parsing X.509
#*    Copyright (C) 2009-2012  CZ.NIC, z.s.p.o. (http://www.nic.cz)
#*
#*    This library is free software; you can redistribute it and/or
#*    modify it under the terms of the GNU Library General Public
#*    License as published by the Free Software Foundation; either
#*    version 2 of the License, or (at your option) any later version.
#*
#*    This library is distributed in the hope that it will be useful,
#*    but WITHOUT ANY WARRANTY; without even the implied warranty of
#*    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#*    Library General Public License for more details.
#*
#*    You should have received a copy of the GNU Library General Public
#*    License along with this library; if not, write to the Free
#*    Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#*

'''
Created on Dec 11, 2009

'''
import base64
import datetime
import time


from pyasn1.error import PyAsn1Error
from pkcs7.asn1_models.tools import *
from pkcs7.asn1_models.oid import *
from pkcs7.asn1_models.tools import *
from pkcs7.asn1_models.X509_certificate import *
from pkcs7.asn1_models.certificate_extensions import *
from pkcs7.debug import *
from pkcs7.asn1_models.decoder_workarounds import decode


class CertificateError(Exception):
    pass


class Name(object):
    '''
    Represents Name (structured, tagged).
    This is a dictionary. Keys are types of names (mapped from OID to name if
    known, see _oid2Name below, otherwise numeric). Values are arrays containing
    the names that mapped to given type (because having more values of one type,
    e.g. multiple CNs is common).
    '''
    _oid2Name = {
        "2.5.4.3": "CN",
        "2.5.4.6": "C",
        "2.5.4.7": "L",
        "2.5.4.8": "ST",
        "2.5.4.10": "O",
        "2.5.4.11": "OU",

        "2.5.4.45": "X500UID",
        "1.2.840.113549.1.9.1": "email",
        "2.5.4.17": "zip",
        "2.5.4.9": "street",
        "2.5.4.15": "businessCategory",
        "2.5.4.5": "serialNumber",
        "2.5.4.43": "initials",
        "2.5.4.44": "generationQualifier",
        "2.5.4.4": "surname",
        "2.5.4.42": "givenName",
        "2.5.4.12": "title",
        "2.5.4.46": "dnQualifier",
        "2.5.4.65": "pseudonym",
        "0.9.2342.19200300.100.1.25": "DC",
        # Spanish FNMT
        "1.3.6.1.4.1.5734.1.2": "Apellido1",
        "1.3.6.1.4.1.5734.1.3": "Apellido2",
        "1.3.6.1.4.1.5734.1.1": "Nombre",
        "1.3.6.1.4.1.5734.1.4": "DNI",
        # http://tools.ietf.org/html/rfc1274.html
        "0.9.2342.19200300.100.1.1": "Userid",
    }

    def __init__(self, name):
        self.__attributes = {}
        for name_part in name:
            for attr in name_part:
                type = str(attr.getComponentByPosition(0).getComponentByName('type'))
                value = str(attr.getComponentByPosition(0).getComponentByName('value'))

                #use numeric OID form only if mapping is not known
                typeStr = Name._oid2Name.get(type) or type
                values = self.__attributes.get(typeStr)
                if values is None:
                    self.__attributes[typeStr] = [value]
                else:
                    values.append(value)

    def __str__(self):
        ''' Returns the Distinguished name as string. The string for the same
        set of attributes is always the same.
        '''
        #There is no consensus whether RDNs in DN are ordered or not, this way
        #we will have all sets having same components mapped to identical string.
        valueStrings = []
        for key in sorted(self.__attributes.keys()):
            values = sorted(self.__attributes.get(key))
            valuesStr = ", ".join(["%s=%s" % (key, value) for value in values])
            valueStrings.append(valuesStr)

        return ", ".join(valueStrings)

    def get_attributes(self):
        return self.__attributes.copy()


class ValidityInterval(object):
    '''
    Validity interval of a certificate. Values are UTC times.
    Attributes:
    -valid_from
    -valid_to
    '''

    def __init__(self, validity):
        self.valid_from = self._getGeneralizedTime(
            validity.getComponentByName("notBefore"))
        self.valid_to = self._getGeneralizedTime(
            validity.getComponentByName("notAfter"))

    def get_valid_from_as_datetime(self):
        return self.parse_date(self.valid_from)

    def get_valid_to_as_datetime(self):
        return self.parse_date(self.valid_to)

    @staticmethod
    def _getGeneralizedTime(timeComponent):
        """Return time from Time component in YYYYMMDDHHMMSSZ format"""
        # !!!! some hack to get signingTime working
        name = ''
        try:
            name = timeComponent.getName()
        except AttributeError:
            pass
        if name == "generalTime":  # from pkcs7.asn1_models.X509_certificate.Time
            # already in YYYYMMDDHHMMSSZ format
            return timeComponent.getComponent()._value
        else:  # utcTime
            # YYMMDDHHMMSSZ format
            # UTCTime has only short year format (last two digits), so add
            # 19 or 20 to make it "full" year; by RFC 5280 it's range 1950..2049
            # !!!! some hack to get signingTime working
            try:
                timeValue = timeComponent.getComponent()._value
            except AttributeError:
                timeValue = str(timeComponent[1][0])
            shortyear = int(timeValue[:2])
            return (shortyear >= 50 and "19" or "20") + timeValue

    @classmethod
    def parse_date(cls, date):
        """
        parses date string and returns a datetime object;
        """
        year = int(date[:4])
        month = int(date[4:6])
        day = int(date[6:8])
        hour = int(date[8:10])
        minute = int(date[10:12])
        try:
            #seconds must be present per RFC 5280, but some braindead certs
            #omit it
            second = int(date[12:14])
        except (ValueError, IndexError):
            second = 0
        if second>59:
            second = 59
        return datetime.datetime(year, month, day, hour, minute, second)


class PublicKeyInfo(object):
    '''
    Represents information about public key.
    Expects RSA or DSA.
    Attributes:
    - alg (OID string identifier of algorithm)
    - key (dict of parameter name to value; keys "mod", "exp" for RSA and
        "pub", "p", "q", "g" for DSA)
    - algType - one of the RSA, DSA "enum" below
    '''
    UNKNOWN = -1
    RSA = 0
    DSA = 1

    def __init__(self, public_key_info):
        algorithm = public_key_info.getComponentByName("algorithm")
        parameters = algorithm.getComponentByName("parameters")

        self.alg = str(algorithm)
        bitstr_key = public_key_info.getComponentByName("subjectPublicKey")

        if self.alg == "1.2.840.113549.1.1.1":
            self.key = get_RSA_pub_key_material(bitstr_key)
            self.algType = PublicKeyInfo.RSA
            self.algName = "RSA"
        elif self.alg == "1.2.840.10040.4.1":
            self.key = get_DSA_pub_key_material(bitstr_key, parameters)
            self.algType = PublicKeyInfo.DSA
            self.algName = "DSA"
        else:
            self.key = {}
            self.algType = PublicKeyInfo.UNKNOWN
            self.algName = self.alg


class SubjectAltNameExt(object):
    '''
    Subject alternative name extension.
    '''
    def __init__(self, asn1_subjectAltName):
        """Parse SubjectAltname"""
        self.items = []
        for gname in asn1_subjectAltName:
            for pos, key in (
                    (0, 'otherName'),
                    (1, 'email'),
                    (2, 'DNS'),
                    (3, 'x400Address'),
                    (4, 'dirName'),
                    (5, 'ediPartyName'),
                    (6, 'URI'),
                    (7, 'IP'),
                    (8, 'RegisteredID')):
                comp = gname.getComponentByPosition(pos)
                if comp:
                    if pos in (0, 3, 5):  # May be wrong
                        value = Name(comp)
                    elif pos == 4:
                        value = Name(comp)
                    else:
                        value = str(comp)
                    self.items.append((key, value))


class BasicConstraintsExt(object):
    '''
    Basic constraints of this certificate - is it CA and maximal chain depth.
    '''
    def __init__(self, asn1_bConstraints):
        self.ca = bool(asn1_bConstraints.getComponentByName("ca")._value)
        self.max_path_len = None
        if asn1_bConstraints.getComponentByName("pathLen") is not None:
            self.max_path_len = asn1_bConstraints.getComponentByName("pathLen")._value


class KeyUsageExt(object):
    '''
    Key usage extension.
    '''
    def __init__(self, asn1_keyUsage):
        self.digitalSignature = False    # (0),
        self.nonRepudiation = False     # (1),
        self.keyEncipherment = False    # (2),
        self.dataEncipherment = False   # (3),
        self.keyAgreement = False       # (4),
        self.keyCertSign = False        # (5),
        self.cRLSign = False            # (6),
        self.encipherOnly = False       # (7),
        self.decipherOnly = False       # (8)

        bits = asn1_keyUsage._value
        try:
            if (bits[0]):
                self.digitalSignature = True
            if (bits[1]):
                self.nonRepudiation = True
            if (bits[2]):
                self.keyEncipherment = True
            if (bits[3]):
                self.dataEncipherment = True
            if (bits[4]):
                self.keyAgreement = True
            if (bits[5]):
                self.keyCertSign = True
            if (bits[6]):
                self.cRLSign = True
            if (bits[7]):
                self.encipherOnly = True
            if (bits[8]):
                self.decipherOnly = True
        except IndexError:
            return


class ExtendedKeyUsageExt(object):
    '''
    Extended key usage extension.
    '''
    #The values of the _keyPurposeAttrs dict will be set to True/False as
    #attributes of this objects depending on whether the extKeyUsage lists them.
    _keyPurposeAttrs = {
        "1.3.6.1.5.5.7.3.1": "serverAuth",
        "1.3.6.1.5.5.7.3.2": "clientAuth",
        "1.3.6.1.5.5.7.3.3": "codeSigning",
        "1.3.6.1.5.5.7.3.4": "emailProtection",
        "1.3.6.1.5.5.7.3.5": "ipsecEndSystem",
        "1.3.6.1.5.5.7.3.6": "ipsecTunnel",
        "1.3.6.1.5.5.7.3.7": "ipsecUser",
        "1.3.6.1.5.5.7.3.8": "timeStamping",
    }

    def __init__(self, asn1_extKeyUsage):
        usageOIDs = set([tuple_to_OID(usageOID) for usageOID in asn1_extKeyUsage])

        for (oid, attr) in ExtendedKeyUsageExt._keyPurposeAttrs.items():
            setattr(self, attr, oid in usageOIDs)


class AuthorityKeyIdExt(object):
    '''
    Authority Key identifier extension.
    Identifies key of the authority which was used to sign this certificate.
    '''
    def __init__(self, asn1_authKeyId):
        if (asn1_authKeyId.getComponentByName("keyIdentifier")) is not None:
            self.key_id = asn1_authKeyId.getComponentByName("keyIdentifier")._value
        if (asn1_authKeyId.getComponentByName("authorityCertSerialNum")) is not None:
            self.auth_cert_sn = asn1_authKeyId.getComponentByName("authorityCertSerialNum")._value
        if (asn1_authKeyId.getComponentByName("authorityCertIssuer")) is not None:
            issuer = asn1_authKeyId.getComponentByName("authorityCertIssuer")
            iss = str(issuer.getComponentByName("name"))
            self.auth_cert_issuer = iss


class SubjectKeyIdExt(object):
    '''
    Subject Key Identifier extension. Just the octet string.
    '''
    def __init__(self, asn1_subKey):
        self.subject_key_id = asn1_subKey._value


class PolicyQualifier(object):
    '''
    Certificate policy qualifier. Consist of id and
    own qualifier (id-qt-cps | id-qt-unotice).
    '''
    def __init__(self, asn1_pQual):
        self.id = tuple_to_OID(asn1_pQual.getComponentByName("policyQualifierId"))
        if asn1_pQual.getComponentByName("qualifier") is not None:
            qual = asn1_pQual.getComponentByName("qualifier")
            self.qualifier = None
            # this is a choice - only one of following types will be non-null

            comp = qual.getComponentByName("cpsUri")
            if comp is not None:
                self.qualifier = str(comp)
            # not parsing userNotice for now
            #comp = qual.getComponentByName("userNotice")
            #if comp is not None:
            #    self.qualifier = comp


class AuthorityInfoAccessExt(object):
    '''
    Authority information access.
    Instance variables:
    - id - accessMethod OID as string
    - access_location as string
    - access_method as string if the OID is known (None otherwise)
    '''
    _accessMethods = {
        "1.3.6.1.5.5.7.48.1": "ocsp",
        "1.3.6.1.5.5.7.48.2": "caIssuers",
    }

    def __init__(self, asn1_authInfo):
        self.id = tuple_to_OID(asn1_authInfo.getComponentByName("accessMethod"))
        self.access_location = str(asn1_authInfo.getComponentByName("accessLocation").getComponent())
        self.access_method = AuthorityInfoAccessExt._accessMethods.get(self.id)
        pass


class CertificatePolicyExt(object):
    '''
    Certificate policy extension.
    COnsist of id and qualifiers.
    '''
    def __init__(self, asn1_certPol):
        self.id = tuple_to_OID(asn1_certPol.getComponentByName("policyIdentifier"))
        self.qualifiers = []
        if (asn1_certPol.getComponentByName("policyQualifiers")):
            qualifiers = asn1_certPol.getComponentByName("policyQualifiers")
            self.qualifiers = [PolicyQualifier(pq) for pq in qualifiers]


class Reasons(object):
    '''
    CRL distribution point reason flags
    '''
    def __init__(self, asn1_rflags):
        self.unused  = False   # (0),
        self.keyCompromise = False   # (1),
        self.cACompromise = False   # (2),
        self.affiliationChanged = False    # (3),
        self.superseded = False   # (4),
        self.cessationOfOperation = False   # (5),
        self.certificateHold = False   # (6),
        self.privilegeWithdrawn = False   # (7),
        self.aACompromise = False   # (8)

        bits = asn1_rflags._value
        try:
            if (bits[0]):
                self.unused = True
            if (bits[1]):
                self.keyCompromise = True
            if (bits[2]):
                self.cACompromise = True
            if (bits[3]):
                self.affiliationChanged = True
            if (bits[4]):
                self.superseded = True
            if (bits[5]):
                self.cessationOfOperation = True
            if (bits[6]):
                self.certificateHold = True
            if (bits[7]):
                self.privilegeWithdrawn = True
            if (bits[8]):
                self.aACompromise = True
        except IndexError:
            return


class CRLdistPointExt(object):
    '''
    CRL distribution point extension
    '''
    def __init__(self, asn1_crl_dp):
        dp = asn1_crl_dp.getComponentByName("distPoint")
        if dp is not None:
            #self.dist_point = str(dp.getComponent())
            self.dist_point = str(dp.getComponentByName("fullName")[0].getComponent())
        else:
            self.dist_point = None
        reasons = asn1_crl_dp.getComponentByName("reasons")
        if reasons is not None:
            self.reasons = Reasons(reasons)
        else:
            self.reasons = None
        issuer = asn1_crl_dp.getComponentByName("issuer")
        if issuer is not None:
            self.issuer = str(issuer)
        else:
            self.issuer = None


class QcStatementExt(object):
    '''
    id_pe_qCStatement
    '''
    def __init__(self, asn1_caStatement):
        self.oid = str(asn1_caStatement.getComponentByName("stmtId"))
        self.statementInfo = asn1_caStatement.getComponentByName("stmtInfo")
        if self.statementInfo is not None:
            self.statementInfo = str(self.statementInfo)


class PolicyConstraintsExt(object):
    def __init__(self, asn1_policyConstraints):
        self.requireExplicitPolicy = None
        self.inhibitPolicyMapping = None

        requireExplicitPolicy = asn1_policyConstraints.getComponentByName("requireExplicitPolicy")
        inhibitPolicyMapping = asn1_policyConstraints.getComponentByName("inhibitPolicyMapping")

        if requireExplicitPolicy is not None:
            self.requireExplicitPolicy = requireExplicitPolicy._value

        if inhibitPolicyMapping is not None:
            self.inhibitPolicyMapping = inhibitPolicyMapping._value


class NameConstraint(object):
    def __init__(self, base, minimum, maximum):
        self.base = base
        self.minimum = minimum
        self.maximum = maximum

    def __repr__(self):
        return "NameConstraint(base: %s, min: %s, max: %s)" % (repr(self.base), self.minimum, self.maximum)

    def __str__(self):
        return self.__repr__()


class NameConstraintsExt(object):
    def __init__(self, asn1_nameConstraints):
        self.permittedSubtrees = []
        self.excludedSubtrees = []

        permittedSubtrees = asn1_nameConstraints.getComponentByName("permittedSubtrees")
        excludedSubtrees = asn1_nameConstraints.getComponentByName("excludedSubtrees")

        self.permittedSubtrees = self._parseSubtree(permittedSubtrees)
        self.excludedSubtrees = self._parseSubtree(excludedSubtrees)

    def _parseSubtree(self, asn1Subtree):
        if asn1Subtree is None:
            return []

        subtreeList = []

        for subtree in asn1Subtree:
            #TODO: somehow extract the type of GeneralName
            base = subtree.getComponentByName("base").getComponent()  # ByName("dNSName")
            if base is None:
                continue

            base = str(base)

            minimum = subtree.getComponentByName("minimum")._value
            maximum = subtree.getComponentByName("maximum")
            if maximum is not None:
                maximum = maximum._value

            subtreeList.append(NameConstraint(base, minimum, maximum))

        return subtreeList


class NetscapeCertTypeExt(object):
    def __init__(self, asn1_netscapeCertType):
        #https://www.mozilla.org/projects/security/pki/nss/tech-notes/tn3.html
        bits = asn1_netscapeCertType._value
        self.clientCert = len(bits) > 0 and bool(bits[0])
        self.serverCert = len(bits) > 1 and bool(bits[1])
        self.caCert = len(bits) > 5 and bool(bits[5])



class AppleSubmissionCertificateExt(object):
    def __init__(self, asn1_type):
        pass


class AppleDevelopmentCertificateExt(object):
    def __init__(self, asn1_type):
        pass


class MacApplicationSoftwareDevelopmentSigning(object):
    def __init__(self, asn1_type):
        pass


class MacApplicationSoftwareSubmissionSigning(object):
    def __init__(self, asn1_type):
        pass


class ExtensionType(object):
    '''"Enum" of extensions we know how to parse.'''
    SUBJ_ALT_NAME = "subjAltNameExt"
    AUTH_KEY_ID = "authKeyIdExt"
    SUBJ_KEY_ID = "subjKeyIdExt"
    BASIC_CONSTRAINTS = "basicConstraintsExt"
    KEY_USAGE = "keyUsageExt"
    EXT_KEY_USAGE = "extKeyUsageExt"
    CERT_POLICIES = "certPoliciesExt"
    CRL_DIST_POINTS = "crlDistPointsExt"
    STATEMENTS = "statemetsExt"
    AUTH_INFO_ACCESS = "authInfoAccessExt"
    POLICY_CONSTRAINTS = "policyConstraintsExt"
    NAME_CONSTRAINTS = "nameConstraintsExt"
    NETSCAPE_CERT_TYPE = "netscapeCertTypeExt"
    APPLE_SUBMISSION_CERTIFICATE = "appleSubmissionCertificateExt"
    APPLE_DEVELOPMENT_CERTIFICATE = "appleDevelopmentCertificateExt"
    MAC_APPLICATION_SOFTWARE_DEVELOPMENT_SIGNING = "macApplicationSoftwareDevelopmentSigningExt"
    MAC_APPLICATION_SOFTWARE_SUBMISSION_SIGNING = "macApplicationSoftwareSubmissionSigningExt"


class ExtensionTypes(object):
    #hackish way to enumerate known extensions without writing them twice
    knownExtensions = [name for (attr, name) in vars(ExtensionType).items() if attr.isupper()]


class Extension(object):
    '''
    Represents one Extension in X509v3 certificate
    Attributes:
    - id  (identifier of extension)
    - is_critical
    - value (value of extension, needs more parsing - it is in DER encoding)
    '''
    #OID: (ASN1Spec, valueConversionFunction, attributeName)
    _extensionDecoders = {
        "2.5.29.17": (GeneralNames(),                 lambda v: SubjectAltNameExt(v),                 ExtensionType.SUBJ_ALT_NAME),
        "2.5.29.35": (KeyId(),                        lambda v: AuthorityKeyIdExt(v),                 ExtensionType.AUTH_KEY_ID),
        "2.5.29.14": (SubjectKeyId(),                 lambda v: SubjectKeyIdExt(v),                   ExtensionType.SUBJ_KEY_ID),
        "2.5.29.19": (BasicConstraints(),             lambda v: BasicConstraintsExt(v),               ExtensionType.BASIC_CONSTRAINTS),
        "2.5.29.15": (None,                           lambda v: KeyUsageExt(v),                       ExtensionType.KEY_USAGE),
        "2.5.29.32": (CertificatePolicies(),          lambda v: [CertificatePolicyExt(p) for p in v], ExtensionType.CERT_POLICIES),
        "2.5.29.31": (CRLDistributionPoints(),        lambda v: [CRLdistPointExt(p) for p in v],      ExtensionType.CRL_DIST_POINTS),
        "1.3.6.1.5.5.7.1.3": (Statements(),           lambda v: [QcStatementExt(s) for s in v],       ExtensionType.STATEMENTS),
        "1.3.6.1.5.5.7.1.1": (AuthorityInfoAccess(),  lambda v: [AuthorityInfoAccessExt(s) for s in v], ExtensionType.AUTH_INFO_ACCESS),
        "2.5.29.37": (ExtendedKeyUsage(),             lambda v: ExtendedKeyUsageExt(v),               ExtensionType.EXT_KEY_USAGE),
        "2.5.29.36": (PolicyConstraints(),            lambda v: PolicyConstraintsExt(v),              ExtensionType.POLICY_CONSTRAINTS),
        "2.5.29.30": (NameConstraints(),              lambda v: NameConstraintsExt(v),                ExtensionType.NAME_CONSTRAINTS),
        "2.16.840.1.113730.1.1": (NetscapeCertType(), lambda v: NetscapeCertTypeExt(v),               ExtensionType.NETSCAPE_CERT_TYPE),
        # From https://images.apple.com/certificateauthority/pdf/Apple_WWDR_CPS_v1.17.pdf
        "1.2.840.113635.100.6.1.4": (None,            lambda v: AppleSubmissionCertificateExt(v),     ExtensionType.APPLE_SUBMISSION_CERTIFICATE),
        "1.2.840.113635.100.6.1.2": (None,            lambda v: AppleDevelopmentCertificateExt(v),     ExtensionType.APPLE_DEVELOPMENT_CERTIFICATE),
        "1.2.840.113635.100.6.1.12": (None,           lambda v: MacApplicationSoftwareDevelopmentSigning(v),     ExtensionType.MAC_APPLICATION_SOFTWARE_DEVELOPMENT_SIGNING),
        "1.2.840.113635.100.6.1.7": (None,            lambda v: MacApplicationSoftwareSubmissionSigning(v),      ExtensionType.MAC_APPLICATION_SOFTWARE_SUBMISSION_SIGNING),
    }

    def __init__(self, extension):
        self.id = tuple_to_OID(extension.getComponentByName("extnID"))
        critical = extension.getComponentByName("critical")
        self.is_critical = (critical != 0)
        self.ext_type = None

        # set the bytes as the extension value
        self.value = extension.getComponentByName("extnValue")._value

        # if we know the type of value, parse it
        decoderTuple = Extension._extensionDecoders.get(self.id)
        if decoderTuple is not None:
            try:
                (decoderAsn1Spec, decoderFunction, extType) = decoderTuple
                v = decode(self.value, asn1Spec=decoderAsn1Spec)[0]
                self.value = decoderFunction(v)
                self.ext_type = extType
            except PyAsn1Error:
                #According to RFC 5280, unrecognized extension can be ignored
                #unless marked critical, though it doesn't cover all cases.
                if self.is_critical:
                    raise
        elif self.is_critical:
            raise CertificateError("Critical extension OID %s not understood" % self.id)


class Certificate(object):
    '''
    Represents Certificate object.
    Attributes:
    - version
    - serial_number
    - signature_algorithm (data are signed with this algorithm)
    - issuer (who issued this certificate)
    - validity
    - subject (for who the certificate was issued)
    - pub_key_info
    - issuer_uid (optional)
    - subject_uid (optional)
    - extensions (list of extensions)
    '''

    def __init__(self, tbsCertificate):
        self.version = tbsCertificate.getComponentByName("version")._value
        self.serial_number = tbsCertificate.getComponentByName("serialNumber")._value
        self.signature_algorithm = str(tbsCertificate.getComponentByName("signature"))
        self.issuer = Name(tbsCertificate.getComponentByName("issuer"))
        self.validity = ValidityInterval(tbsCertificate.getComponentByName("validity"))
        self.subject = Name(tbsCertificate.getComponentByName("subject"))
        self.pub_key_info = PublicKeyInfo(tbsCertificate.getComponentByName("subjectPublicKeyInfo"))

        issuer_uid = tbsCertificate.getComponentByName("issuerUniqueID")
        if issuer_uid:
            self.issuer_uid = issuer_uid.toOctets()
        else:
            self.issuer_uid = None

        subject_uid = tbsCertificate.getComponentByName("subjectUniqueID")
        if subject_uid:
            self.subject_uid = subject_uid.toOctets()
        else:
            self.subject_uid = None

        self.extensions = self._create_extensions_list(tbsCertificate.getComponentByName('extensions'))

        #make known extensions accessible through attributes
        for extAttrName in ExtensionTypes.knownExtensions:
            setattr(self, extAttrName, None)
        for ext in self.extensions:
            if ext.ext_type:
                setattr(self, ext.ext_type, ext)

    def _create_extensions_list(self, extensions):
        if extensions is None:
            return []

        return [Extension(ext) for ext in extensions]


class X509Certificate(object):
    '''
    Represents X509 certificate.
    Attributes:
    - signature_algorithm (used to sign this certificate)
    - signature
    - tbsCertificate (the certificate)
    '''

    def __init__(self, certificate):
        self.signature_algorithm = str(certificate.getComponentByName("signatureAlgorithm"))
        self.signature = certificate.getComponentByName("signatureValue").toOctets()
        tbsCert = certificate.getComponentByName("tbsCertificate")
        self.tbsCertificate = Certificate(tbsCert)
        self.verification_results = None
        self.raw_der_data = ""  # raw der data for storage are kept here by cert_manager
        self.check_crl = True

    def is_verified(self, ignore_missing_crl_check=False):
        '''
        Checks if all values of verification_results dictionary are True,
        which means that the certificate is valid
        '''
        return self._evaluate_verification_results(
                        self.verification_results,
                        ignore_missing_crl_check=ignore_missing_crl_check)

    def valid_at_date(self, date, ignore_missing_crl_check=False):
        """check validity of all parts of the certificate with regard
        to a specific date"""
        verification_results = self.verification_results_at_date(date)
        return self._evaluate_verification_results(
                        verification_results,
                        ignore_missing_crl_check=ignore_missing_crl_check)

    def _evaluate_verification_results(self, verification_results,
                                       ignore_missing_crl_check=False):
        if verification_results is None:
            return False
        for key, value in verification_results.iteritems():
            if value:
                pass
            elif ignore_missing_crl_check and key == "CERT_NOT_REVOKED" and value is None:
                continue
            else:
                return False
        return True

    def verification_results_at_date(self, date):
        if self.verification_results is None:
            return None
        results = dict(self.verification_results)   # make a copy
        results["CERT_TIME_VALIDITY_OK"] = self.time_validity_at_date(date)
        if self.check_crl:
            results["CERT_NOT_REVOKED"] = self.crl_validity_at_date(date)
        else:
            results["CERT_NOT_REVOKED"] = None
        return results

    def time_validity_at_date(self, date):
        """check if the time interval of validity of the certificate contains
        'date' provided as argument"""
        from_date = self.tbsCertificate.validity.get_valid_from_as_datetime()
        to_date = self.tbsCertificate.validity.get_valid_to_as_datetime()
        time_ok = to_date >= date >= from_date
        return time_ok

    def crl_validity_at_date(self, date):
        """check if the certificate was not on the CRL list at a particular date"""
        rev_date = self.get_revocation_date()
        if not rev_date:
            return True
        if date >= rev_date:
            return False
        else:
            return True

    def get_revocation_date(self):
        from certs.crl_store import CRL_cache_manager
        cache = CRL_cache_manager.get_cache()
        issuer = str(self.tbsCertificate.issuer)
        rev_date = cache.certificate_rev_date(issuer, self.tbsCertificate.serial_number)
        if not rev_date:
            return None
        rev_date = ValidityInterval.parse_date(rev_date)
        return rev_date


class Attribute(object):
    """
    One attribute in SignerInfo attributes set
    """
    _oid2Name = {
        "1.2.840.113549.1.9.1": "emailAddress",
        "1.2.840.113549.1.9.2": "unstructuredName",
        "1.2.840.113549.1.9.3": "contentType",
        "1.2.840.113549.1.9.4": "messageDigest",
        "1.2.840.113549.1.9.5": "signingTime",
        "1.2.840.113549.1.9.6": "counterSignature",
        "1.2.840.113549.1.9.7": "challengePassword",
        "1.2.840.113549.1.9.8": "unstructuredAddress",
        "1.2.840.113549.1.9.16.2.12": "signingCertificate",
        "2.5.4.5": "serialNumber",
    }

    def __init__(self, attribute):
        self.type = str(attribute.getComponentByName("type"))
        self.value = attribute.getComponentByName("value").getComponentByPosition(0)
        self.name = self._oid2Name.get(self.type, self.type)
        if self.name == 'signingTime':
            self.value = ValidityInterval.parse_date(
                ValidityInterval._getGeneralizedTime(attribute))

    def __str__(self):
        value = str(self.value)
        if self.name == 'messageDigest':
            value = base64.standard_b64encode(value)
        elif self.name == 'signingCertificate':
            value = SigningCertificate(self.value)
        elif self.name == 'contentType':
            value = ContentType(value)
        elif self.name == 'serialNumber':
            value = "0x%x" % long(str(self.value))
        return "%s: %s" % (self.name, value)


class ContentType(object):
    """
    PKCS 7 content type
    """
    _oid2Name = {
        "1.2.840.113549.1.7.1": "data",
        "1.2.840.113549.1.7.2": "signedData",
        "1.2.840.113549.1.7.3": "envelopedData",
        "1.2.840.113549.1.7.4": "signedAndEnvelopedData",
        "1.2.840.113549.1.7.5": "digestedData",
        "1.2.840.113549.1.7.6": "encryptedData",
    }

    def __init__(self, data):
        self.value = data

    def __str__(self):
        return self._oid2Name.get(self.value, self.value)


class SigningCertificate(object):
    """
    Sequence of certs and policies defined in RFC 2634

    SigningCertificate ::=  SEQUENCE {
       certs        SEQUENCE OF ESSCertID,
       policies     SEQUENCE OF PolicyInformation OPTIONAL
    }
    """
    def __init__(self, data):
        self.certs = []
        for cert in data.getComponentByPosition(0):
            self.certs.append(ESSCertID(cert))
        self.policies = []
        try:
            self.policies = data.getComponentByPosition(1)
        except IndexError:
            pass

    def __str__(self):
        return ','.join([str(cert) for cert in self.certs])


class ESSCertID(object):
    """
    Certificate identifier RFC 2634

    ESSCertID ::=  SEQUENCE {
        certHash                 Hash,
        issuerSerial             IssuerSerial OPTIONAL
    }

    Hash ::= OCTET STRING -- SHA1 hash of entire certificate

    IssuerSerial ::= SEQUENCE {
        issuer                   GeneralNames,
        serialNumber             CertificateSerialNumber
    }
    """
    def __init__(self, data):
        self.hash = data.getComponentByPosition(0)
        self.issuer = data.getComponentByPosition(1).getComponentByPosition(0)
        self.serial_number = data.getComponentByPosition(1).getComponentByPosition(1)._value

    def __str__(self):
        return "0x%x" % self.serial_number

class AutheticatedAttributes(object):
    """
    Authenticated attributes of signer info
    """
    def __init__(self, auth_attributes):
        self.attributes = []
        for aa in auth_attributes:
            self.attributes.append(Attribute(aa))


class SignerInfo(object):
    """
    Represents information about a signer.
    Attributes:
    - version
    - issuer
    - serial_number (of the certificate used to verify this signature)
    - digest_algorithm
    - encryp_algorithm
    - signature
    - auth_atributes (optional field, contains authenticated attributes)
    """
    def __init__(self, signer_info):
        self.version = signer_info.getComponentByName("version")._value
        self.issuer = Name(signer_info.getComponentByName("issuerAndSerialNum").getComponentByName("issuer"))
        self.serial_number = signer_info.getComponentByName("issuerAndSerialNum").getComponentByName("serialNumber")._value
        self.digest_algorithm = str(signer_info.getComponentByName("digestAlg"))
        self.encrypt_algorithm = str(signer_info.getComponentByName("encryptAlg"))
        self.signature = signer_info.getComponentByName("signature")._value
        auth_attrib = signer_info.getComponentByName("authAttributes")
        if auth_attrib is None:
            self.auth_attributes = None
        else:
            self.auth_attributes = AutheticatedAttributes(auth_attrib)


######
#TSTinfo
######
class MsgImprint(object):
    def __init__(self, asn1_msg_imprint):
        self.alg = str(asn1_msg_imprint.getComponentByName("algId"))
        self.imprint = str(asn1_msg_imprint.getComponentByName("imprint"))


class TsAccuracy(object):
    def __init__(self, asn1_acc):
        secs = asn1_acc.getComponentByName("seconds")
        if secs:
            self.seconds = secs._value
        milis = asn1_acc.getComponentByName("milis")
        if milis:
            self.milis = milis._value
        micros = asn1_acc.getComponentByName("micros")
        if micros:
            self.micros = micros._value


class TimeStampToken(object):
    '''
    Holder for Timestamp Token Info - attribute from the qtimestamp.
    '''
    def __init__(self, asn1_tstInfo):
        self.version = asn1_tstInfo.getComponentByName("version")._value
        self.policy = str(asn1_tstInfo.getComponentByName("policy"))
        self.msgImprint = MsgImprint(asn1_tstInfo.getComponentByName("messageImprint"))
        self.serialNum = asn1_tstInfo.getComponentByName("serialNum")._value
        self.genTime = asn1_tstInfo.getComponentByName("genTime")._value
        self.accuracy = TsAccuracy(asn1_tstInfo.getComponentByName("accuracy"))
        self.tsa = Name(asn1_tstInfo.getComponentByName("tsa"))
        # place for parsed certificates in asn1 form
        self.asn1_certificates = []
        # place for certificates transformed to X509Certificate
        self.certificates = []
        #self.extensions = asn1_tstInfo.getComponentByName("extensions")

    def certificates_contain(self, cert_serial_num):
        """
        Checks if set of certificates of this timestamp contains
        certificate with specified serial number.
        Returns True if it does, False otherwise.
        """
        for cert in self.certificates:
            if cert.tbsCertificate.serial_number == cert_serial_num:
                return True
        return False

    def get_genTime_as_datetime(self):
        """
        parses the genTime string and returns a datetime object;
        it also adjusts the time according to local timezone, so that it is
        compatible with other parts of the library
        """
        year = int(self.genTime[:4])
        month = int(self.genTime[4:6])
        day = int(self.genTime[6:8])
        hour = int(self.genTime[8:10])
        minute = int(self.genTime[10:12])
        second = int(self.genTime[12:14])
        rest = self.genTime[14:].strip("Z")
        if rest:
            micro = int(float(rest) * 1e6)
        else:
            micro = 0
        tz_delta = datetime.timedelta(seconds=time.daylight and time.altzone
                                        or time.timezone)
        return datetime.datetime(year, month, day, hour, minute, second, micro) - tz_delta
