import MySQLdb
from tkmail.config import HOSTNAME, USERNAME, PASSWORD, DATABASE
import pickle
import base64


class Database(object):
    def __init__(self):
        self._mysql = MySQLdb.connect(host=HOSTNAME, user=USERNAME,
                                      passwd=PASSWORD, db=DATABASE)
        self._cursor = self._mysql.cursor()

    def _execute(self, statement, *args):
        if args:
            sql = statement % args
        else:
            sql = statement

        self._cursor.execute(sql)

    def _fetchall(self, *args, **kwargs):
        column = kwargs.pop('column', None)
        self._execute(*args)
        rows = self._cursor.fetchall()
        if column is not None:
            return [row[column] for row in rows]
        else:
            return list(rows)

    def get_email_addresses(self, id_list):
        id_string = ','.join(str(each) for each in id_list)
        return dict(self._fetchall("""
            SELECT `id`, `email` FROM `idm_profile`
            WHERE `id` IN (%s)
            AND `allow_direct_email` = TRUE
            AND `email` != ""
            """, id_string))

    def get_admin_emails(self):
        return self._fetchall("""
            SELECT `idm_profile`.`email`
            FROM `idm_profile`, `idm_group`, `idm_profile_groups`
            WHERE `idm_group`.name = "ADMIN"
            AND `idm_profile_groups`.`group_id`=`idm_group`.`id`
            AND `idm_profile_groups`.`profile_id`= `idm_profile`.`id`
            """, column=0)

    def get_groups(self):
        """Get the groups.

        >>> db = Database()
        >>> sorted(db.get_groups())
        ... # TODO: fix output to include name
        [(3, 'FUCK'), (4, 'HEST'), (5, 'KET'),
        (6, 'SPIRIL(?:L?EN)?'), (7, '(?:FORM)?JUNTA(?:EN)?'),
        (8, 'N(?:AEST)?FORMATION(?:EN)?'), (9, '(?:CERM)?LAUG(?:ET)?'),
        (10, 'REVY(?:EN)?'), (11, 'J50'), (12, '(ENGINEERING|TK-?E)'),
        (13, 'ADMIN(?:ISTRATOR(?:ERNE|EN)?)?'),
        (21, 'WEB(?:MASTER(?:EN|NE)?)?'), (22, 'REVYBAND(?:ET)?'),
        (23, 'REVYCREW'), (24, 'REVYSPAM'), (25, 'REVIS(ION|OR)'),
        (27, 'G*S?SR'), (28, 'REVYTEKNIK'), (30, 'FILF'), (110, 'BEST'),
        (111, 'BESTFU'), (112, 'FU'), (113, 'FU(?!CK|LD)[A-Z]{2),
        (114, 'EFU[A-Z]{2), (115, 'FORM'), (116, 'NF'), (117, 'CERM'),
        (118, 'VC'), (119, 'SEKR'), (120, 'PR'), (121, 'KASS'),
        (122, 'USERID[0-9]+'), (126, '(8|OTT(END)?E)'), (128, 'J60'),
        (129, 'J60KOOR'), (130, 'INKA'), (131, 'HAPPENING'), (132, 'TKIT'),
        (134, '(TK)?SY'), (136, 'ABEN'), (137, 'J60REVY')]
        """

        return self._fetchall("""
            SELECT `id`, `name`, `regexp` FROM idm_group
            """)

    def get_group_members(self, group_id):
        return self._fetchall("""
            SELECT `profile_id` FROM `idm_profile_groups`
            WHERE `group_id`='%s'
            """, group_id, column=0)

    def get_current_bestfu_members(self, kind, period):
        if kind == "BEST" and period == 2022 and self.get_ginka_standin_2022():
            best = self.get_bestfu_members(kind, period)
            rm = self.get_user_by_title("INKA", period)
            return [b for b in best if b not in rm]
        return self.get_bestfu_members(kind, period)

    def get_current_bestfu_member(self, title, period):
        if title == "INKA" and period == 2022 and self.get_ginka_standin_2022():
            return self.get_user_by_title(title, 2021)
        return self.get_user_by_title(title, period)

    def get_bestfu_members(self, kind, period):
        assert kind in ('BEST', 'FU', 'EFU')
        return self._fetchall("""
            SELECT `profile_id` FROM `idm_title`
            WHERE `period` = '%s' AND `kind` = '%s'
            """, period, kind, column=0)

    def get_user_by_title(self, title, period):
        return self._fetchall("""
            SELECT `profile_id` FROM `idm_title`
            WHERE `root` = '%s' AND `period` = '%s'
            """, title, period, column=0)

    def get_user_by_id(self, user_id):
        return self._fetchall("""
            SELECT `id` FROM `idm_profile`
            WHERE `id`='%s'
            """, user_id, column=0)

    def get_all_best(self, period):
        """Get all BEST members.

        >>> db = Database()
        >>> db.get_all_best(2014)
        [('Martin Sand Nielsen', 'FORM', 0),
        ('Jacob Albæk Schnedler ', 'NF', 1),
        ('Henrik Lund Mortensen ', 'INKA', 2),
        ('Mathias Jaquet Mavraganis', 'KA$$', 3),
        ('Jonas Kielsholm', 'CERM', 4),
        ('Peter Lystlund Matzen', 'VC', 5),
        ('Alexandra Fabricius Porsgaard', 'PR', 6),
        ('Camilla Ulbæk Pedersen', 'SEKR', 7)]
        """

        best_order = 'FORM NF INKA KASS CERM VC PR SEKR'.split()

        best_rows = self._fetchall("""
            SELECT idm_profile.name, idm_title.root
            FROM idm_profile, idm_title
            WHERE idm_title.period = '%s'
            AND idm_profile.id = idm_title.profile_id
            AND idm_title.kind = 'BEST'
        """, period)
        best = [(name, title, best_order.index(title.replace('$', 'S')))
                for name, title in best_rows]
        best.sort(key=lambda x: x[2])
        return best

    def get_current_period(self):
        rows = self._fetchall("""
            SELECT `value` FROM `constance_config` WHERE `key` = "GFYEAR"
        """)
        if len(rows) == 0:
            raise Exception("No 'GFYEAR' in constance_config")
        value = rows[0][0]
        value = base64.b64decode(value)
        value = pickle.loads(value)
        return value

    def get_ginka_standin_2022(self):
        rows = self._fetchall("""
            SELECT `value` FROM `constance_config` WHERE `key` = "GINKA_STANDIN_2022"
        """)
        if len(rows) == 0:
            return False
        value = rows[0][0]
        value = base64.b64decode(value)
        value = pickle.loads(value)
        return value
